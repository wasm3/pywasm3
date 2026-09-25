#include "Python.h"

#include <stdio.h>
#include <string.h>

#include "wasm3.h"
#include "m3_env.h"     // for fields the public API has no getter for: M3Module.numMemories,
                        // M3Runtime.isSuspendable and .lastCalled
#include "m3_host.h"    // m3_HostGuardsActive()

#define MAX_ARGS 32

// PyUnicode_AsUTF8() is stable ABI only since 3.13; this one since 3.10.
static const char *
as_utf8(PyObject *str)
{
    return PyUnicode_AsUTF8AndSize(str, NULL);
}

#ifdef Py_GIL_DISABLED
// A recursive lock: whatever a guest calls back into (an import reading the guest's
// memory, calling another of its functions) runs on the thread that already holds it.
typedef struct {
    PyMutex     mutex;
    uintptr_t   owner;      // the holding thread's ident, 0 when free
    int         depth;
} m3_lock;
#endif

typedef struct {
    PyObject_HEAD
    IM3Environment e;
#ifdef Py_GIL_DISABLED
    m3_lock lock;
#endif
} m3_environment;

typedef struct {
    PyObject_HEAD
    m3_environment *env;
    IM3Runtime r;
    // Whatever a loaded module needs to outlive its Module object: the bytes wasm3
    // parsed it from, and the callables linked into its imports.
    PyObject *keepalive;
} m3_runtime;

typedef struct {
    PyObject_HEAD
    m3_environment *env;
    m3_runtime *runtime;
    IM3Module m;
    PyObject *bytes;
    PyObject *linked;       // the callables link_function() handed to wasm3
    //bool is_gas_metered;
    int64_t total_gas;
    int64_t current_gas;
} m3_module;

typedef struct {
    PyObject_HEAD
    IM3Function f;
    IM3Runtime r;
    m3_runtime *runtime;
} m3_function;

typedef struct {
    PyObject_HEAD
    m3_module *module;
    uint32_t index;
} m3_memory;

static PyObject *M3_Environment_Type;
static PyObject *M3_Runtime_Type;
static PyObject *M3_Module_Type;
static PyObject *M3_Function_Type;
static PyObject *M3_Memory_Type;

static PyObject *call_outcome(IM3Runtime runtime, IM3Function f, M3Result err);
// What an import that raised traps with: the Python exception is already set
static const char* trapException = "function raised exception";

// Without a GIL, nothing else keeps two threads out of wasm3, which is not thread-safe.
// The unit of locking is the Environment, not the Runtime: runtimes share their
// environment's type table and code pages, and write to both whenever they compile -
// which a call does too, as it reaches functions that were not compiled yet. So
// runtimes in separate environments run in parallel, those sharing one take turns.
//
// A PyMutex rather than Py_BEGIN_CRITICAL_SECTION: a critical section is let go
// whenever the thread blocks, which Python code in an import may well do, and another
// thread would get into the runtime in the middle of the call.
#ifdef Py_GIL_DISABLED
static void
env_lock(m3_environment *env)
{
    if (!env) return;
    uintptr_t me = (uintptr_t)PyThread_get_thread_ident();
    // Only this thread ever stores its own ident, so seeing it means it holds the lock
    if (_Py_atomic_load_uintptr_relaxed(&env->lock.owner) == me) {
        env->lock.depth++;
        return;
    }
    PyMutex_Lock(&env->lock.mutex);     // detaches from the interpreter while it waits
    _Py_atomic_store_uintptr_relaxed(&env->lock.owner, me);
    env->lock.depth = 1;
}

static void
env_unlock(m3_environment *env)
{
    if (!env) return;
    if (--env->lock.depth == 0) {
        _Py_atomic_store_uintptr_relaxed(&env->lock.owner, 0);
        PyMutex_Unlock(&env->lock.mutex);
    }
}

// Guarded memory hands out slots of a single arena for the whole process, and leaves
// keeping two threads out of it to the embedder (see d_m3GuardedMemory in m3_config.h),
// so this goes around whatever takes or gives back a slot - loading a module, freeing a
// runtime or a module, restoring a snapshot - across environments. It is always the
// innermost lock, and no Python code runs under it.
// TODO: move this into wasm3 itself - lock Guard_TakeSlot()/Guard_GiveSlot() and make
// the POSIX fault handler install (install_guard_handlers) happen once - then drop
// arena_mutex and the m3_HostGuardsActive() call in M3_Runtime_load_unlocked().
static PyMutex arena_mutex;
#define arena_lock()        PyMutex_Lock(&arena_mutex)
#define arena_unlock()      PyMutex_Unlock(&arena_mutex)
#else
#define env_lock(env)       ((void)0)
#define env_unlock(env)     ((void)0)
#define arena_lock()        ((void)0)
#define arena_unlock()      ((void)0)
#endif

// Defines NAME as NAME##_unlocked run under the lock of the environment ENV names,
// for the methods of the PyObject *(self, PyObject *) shape
#define WITH_ENV_LOCK(NAME, SELF_TYPE, ENV)             \
    static PyObject *                                   \
    NAME(SELF_TYPE *self, PyObject *arg)                \
    {                                                   \
        m3_environment *env = (ENV);                    \
        env_lock(env);                                  \
        PyObject *result = NAME##_unlocked(self, arg);  \
        env_unlock(env);                                \
        return result;                                  \
    }


m3ApiRawFunction(metering_usegas)
{
    m3ApiGetArg     (int32_t, gas)

    m3_module *mod = (m3_module *)(_ctx->userdata);

    mod->current_gas -= gas;

    if (M3_UNLIKELY(mod->current_gas < 0)) {
        m3ApiTrap("[trap] Out of gas");
    }
    m3ApiSuccess();
}


// Allocates through the type handed in, not M3_Environment_Type, so that a Python
// subclass (wasm3.Environment, which adds WAT support) gets an instance of itself,
// with room for its __dict__ - PyObject_New() would size and tag it as the base type.
static PyObject *
newEnvironment(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    m3_environment *self = (m3_environment *)PyType_GenericAlloc(type, 0);
    if (!self) return NULL;
    self->e = m3_NewEnvironment();
    if (!self->e) {
        Py_DECREF((PyObject *)self);
        return PyErr_NoMemory();
    }
    return (PyObject *)self;
}

static void
delEnvironment(m3_environment *self)
{
    m3_FreeEnvironment(self->e);
    self->e = NULL;
}

static PyObject *
formatError(PyObject *exception, IM3Runtime runtime, M3Result err)
{
    M3ErrorInfo info;
    memset(&info, 0, sizeof(info));
    m3_GetErrorInfo (runtime, &info);
    // A module that is not loaded into a runtime yet has no runtime to carry error
    // info, so m3_GetErrorInfo leaves the message NULL
    if (info.message && strlen(info.message)) {
        PyErr_Format(exception, "%s (%s)", err, info.message);
    } else {
        PyErr_SetString(exception, err);
    }
    return NULL;
}

static void
put_arg_on_stack(uint64_t *s, M3ValueType type, PyObject *arg)
{
    switch (type) {
        case c_m3Type_i32:  *(int32_t*)(s) = PyLong_AsLong(arg);     break;
        case c_m3Type_i64:  *(int64_t*)(s) = PyLong_AsLongLong(arg); break;
        case c_m3Type_f32:  *(float*)(s)   = PyFloat_AsDouble(arg);  break;
        case c_m3Type_f64:  *(double*)(s)  = PyFloat_AsDouble(arg);  break;
    }
}

static PyObject *
get_arg_from_stack(uint64_t *s, M3ValueType type)
{
    switch (type) {
        case c_m3Type_i32:  return PyLong_FromLong(     *(int32_t*)s);  break;
        case c_m3Type_i64:  return PyLong_FromLongLong( *(int64_t*)s);  break;
        case c_m3Type_f32:  return PyFloat_FromDouble(  *(float*)s);    break;
        case c_m3Type_f64:  return PyFloat_FromDouble(  *(double*)s);   break;
        default:
            return PyErr_Format(PyExc_TypeError, "unknown type %d", (int)type);
    }
}

static int
set_tagged_value(M3TaggedValue *tagged, M3ValueType type, PyObject *value)
{
    tagged->type = type;

    switch (tagged->type) {
        case c_m3Type_i32:
            tagged->value.i32 = PyLong_AsLong(value);
            break;
        case c_m3Type_i64:
            tagged->value.i64 = PyLong_AsLongLong(value);
            break;
        case c_m3Type_f32:
            tagged->value.f32 = PyFloat_AsDouble(value);
            break;
        case c_m3Type_f64:
            tagged->value.f64 = PyFloat_AsDouble(value);
            break;
        default:
            PyErr_Format(PyExc_TypeError, "unknown type %d", (int)tagged->type);
            return -1;
    }

    return PyErr_Occurred() ? -1 : 0;
}

static PyObject *
M3_Environment_new_runtime_unlocked(m3_environment *env, PyObject *stack_size_bytes)
{
    size_t n = PyLong_AsSize_t(stack_size_bytes);
    if (PyErr_Occurred()) {
        return NULL;
    }

    m3_runtime *self = PyObject_New(m3_runtime, (PyTypeObject*)M3_Runtime_Type);
    if (!self) return NULL;
    Py_INCREF((PyObject *)env);
    self->env = env;
    self->r = m3_NewRuntime(env->e, n, NULL);
    self->keepalive = PyList_New(0);
    if (!self->r || !self->keepalive) {
        Py_DECREF((PyObject *)self);
        PyErr_NoMemory();
        return NULL;
    }
    return (PyObject *)self;
}

static void
delRuntime(m3_runtime *self)
{
    // Hands its code pages back to the environment
    env_lock(self->env);
    arena_lock();
    m3_FreeRuntime(self->r);
    arena_unlock();
    env_unlock(self->env);
    self->r = NULL;
    Py_XDECREF(self->keepalive);
    self->keepalive = NULL;
    Py_XDECREF((PyObject *)self->env);
    self->env = NULL;
}

static PyObject *
M3_Environment_parse_module_unlocked(m3_environment *env, PyObject *bytes)
{
    Py_ssize_t size;
    char *data;
    if (PyBytes_AsStringAndSize(bytes, &data, &size) < 0) {
        return NULL;
    }

    IM3Module m;
    M3Result err = m3_ParseModule(env->e, &m, data, size);
    if (err) {
        PyErr_SetString(PyExc_RuntimeError, err);
        return NULL;
    }
    m3_module *self = PyObject_New(m3_module, (PyTypeObject*)M3_Module_Type);
    if (!self) {
        m3_FreeModule(m);
        return NULL;
    }
    // PyObject_New() hands back uninitialized memory, so every field the finalizer
    // touches goes in before anything that can fail.
    self->env = NULL;
    self->runtime = NULL;
    self->m = m;
    self->bytes = NULL;
    self->linked = NULL;
    self->total_gas = self->current_gas = 0;

    self->linked = PyList_New(0);
    if (!self->linked) {
        Py_DECREF((PyObject *)self);        // frees the module through delModule()
        return NULL;
    }
    Py_INCREF(bytes);
    self->bytes = bytes;
    Py_INCREF((PyObject *)env);
    self->env = env;
    return (PyObject *)self;
}

WITH_ENV_LOCK(M3_Environment_new_runtime,   m3_environment, self)
WITH_ENV_LOCK(M3_Environment_parse_module,  m3_environment, self)

static PyMethodDef M3_Environment_methods[] = {
    {"new_runtime",            (PyCFunction)M3_Environment_new_runtime,  METH_O,
        PyDoc_STR("new_runtime(stack_size_bytes) -> Runtime")},
    {"parse_module",            (PyCFunction)M3_Environment_parse_module,  METH_O,
        PyDoc_STR("new_runtime(bytes) -> Module")},
    {NULL,              NULL}           /* sentinel */
};

static PyType_Slot M3_Environment_Type_slots[] = {
    {Py_tp_doc, "The wasm3.Environment type"},
    {Py_tp_finalize, delEnvironment},
    {Py_tp_new, newEnvironment},
    {Py_tp_methods, M3_Environment_methods},
    {0, 0}
};

static PyObject *
M3_Runtime_load_unlocked(m3_runtime *runtime, PyObject *arg)
{
    if (!PyObject_TypeCheck(arg, (PyTypeObject *)M3_Module_Type)) {
        PyErr_SetString(PyExc_TypeError, "load expects a Module");
        return NULL;
    }

    m3_module *module = (m3_module *)arg;

    // Its types are canonical within the environment it was parsed in, and compiling it
    // writes there - under that environment's lock, which is the one this call holds.
    if (module->env != runtime->env) {
        PyErr_SetString(PyExc_RuntimeError, "module was parsed in a different environment");
        return NULL;
    }

    // A loaded module belongs to the runtime, which may outlive the Module object, so
    // the runtime takes over keeping its bytes and its linked callables alive. The list
    // itself is shared, so functions linked after this still end up covered.
    Py_ssize_t keep_index = PyList_Size(runtime->keepalive);
    if (PyList_Append(runtime->keepalive, module->bytes) < 0) {
        return NULL;
    }
    if (PyList_Append(runtime->keepalive, module->linked) < 0) {
        PySequence_DelItem(runtime->keepalive, keep_index);
        return NULL;
    }

    arena_lock();
#if d_m3GuardedMemory
    // On POSIX, the first protected call installs the fault handlers - and two of them
    // racing to do it would each chain to the other. Settling it here, before anything
    // loaded can be called, keeps it under this lock.
    m3_HostGuardsActive();
#endif
    M3Result err = m3_LoadModule(runtime->r, module->m);
    arena_unlock();
    if (err == m3Err_moduleAlreadyLinked) {
        PySequence_DelItem(runtime->keepalive, keep_index + 1);
        PySequence_DelItem(runtime->keepalive, keep_index);
        return formatError(PyExc_RuntimeError, runtime->r, err);
    }

    // A load that fails past that point still leaves the runtime owning the module (its
    // functions may already sit in another module's table), so it is loaded all the same
    Py_INCREF((PyObject *)runtime);
    module->runtime = runtime;
    if (err) {
        return formatError(PyExc_RuntimeError, runtime->r, err);
    }

    err = m3_LinkRawFunctionEx (module->m, "metering", "usegas", "v(i)", &metering_usegas, module);
    /*if (!err) {
        self->is_gas_metered = true;
    }*/
    if (err && err != m3Err_functionLookupFailed) {
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(module->m), err);
    }

    Py_RETURN_NONE;
}

static PyObject *
M3_Runtime_find_function_unlocked(m3_runtime *runtime, PyObject *name)
{
    IM3Function func = NULL;
    M3Result err = m3_FindFunction(&func, runtime->r, as_utf8(name));
    if (err) {
        return formatError(PyExc_RuntimeError, runtime->r, err);
    }
    m3_function *self = PyObject_New(m3_function, (PyTypeObject*)M3_Function_Type);
    if (!self) return NULL;
	Py_INCREF((PyObject *)runtime);
    self->f = func;
    self->r = runtime->r;
    self->runtime = runtime;
    return (PyObject *)self;
}

static int
set_resource_limit(m3_runtime *self, M3ResourceLimit limit, uint64_t value)
{
    env_lock(self->env);
    M3Result err = m3_SetResourceLimit(self->r, limit, value);
    env_unlock(self->env);
    if (err) {
        PyErr_SetString(err == m3Err_resourceLimitBelowUsage ? PyExc_ValueError : PyExc_RuntimeError, err);
        return -1;
    }
    return 0;
}

// Native gas metering: wasm3 instruments bodies as it compiles them, so the limit
// must be set before anything runs (find_function compiles, and runs the start function).
// wasm3 counts whole units, M3_GAS_UNITS_PER_GAS to a gas; Python speaks in gas.
static int
Runtime_setGasLimit(m3_runtime *self, PyObject *value, void * closure)
{
    if (!value) {
        PyErr_SetString(PyExc_AttributeError, "cannot delete gas_limit");
        return -1;
    }
    double gas = PyFloat_AsDouble(value);
    if (gas == -1.0 && PyErr_Occurred()) {
        return -1;
    }
    // Saturates rather than overflowing the cast; wasm3 then saturates at INT64_MAX.
    // Negatives (and NaN) arm an empty budget, as 0 does.
    uint64_t units;
    if (gas >= (double)UINT64_MAX / M3_GAS_UNITS_PER_GAS) {
        units = UINT64_MAX;
    } else if (gas > 0) {
        units = (uint64_t)(gas * M3_GAS_UNITS_PER_GAS);
    } else {
        units = 0;
    }
    return set_resource_limit(self, c_m3Limit_GasUnits, units);
}

static PyObject *
Runtime_getGasLimit(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    uint64_t units = m3_GetResourceLimit(self->r, c_m3Limit_GasUnits);
    env_unlock(self->env);
    return PyFloat_FromDouble((double)units / M3_GAS_UNITS_PER_GAS);
}

static PyObject *
Runtime_getGasUsed(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    uint64_t units = m3_GetResourceUsage(self->r, c_m3Limit_GasUnits);
    env_unlock(self->env);
    return PyFloat_FromDouble((double)units / M3_GAS_UNITS_PER_GAS);
}

// Allocation caps: memory bytes, table elements, continuation stacks. The closure is the
// M3ResourceLimit. Totals across every module loaded into the runtime, 0 is unlimited,
// and a cap below what is already in use is refused.
static int
Runtime_setResourceLimit(m3_runtime *self, PyObject *value, void * closure)
{
    if (!value) {
        PyErr_SetString(PyExc_AttributeError, "cannot delete a resource limit");
        return -1;
    }
    PyObject *index = PyNumber_Index(value);
    if (!index) {
        return -1;
    }
    unsigned long long limit = PyLong_AsUnsignedLongLong(index);
    Py_DECREF(index);
    if (limit == (unsigned long long)-1 && PyErr_Occurred()) {
        return -1;
    }
    return set_resource_limit(self, (M3ResourceLimit)(intptr_t)closure, limit);
}

static PyObject *
Runtime_getResourceLimit(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    uint64_t limit = m3_GetResourceLimit(self->r, (M3ResourceLimit)(intptr_t)closure);
    env_unlock(self->env);
    return PyLong_FromUnsignedLongLong(limit);
}

static PyObject *
Runtime_getResourceUsage(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    uint64_t used = m3_GetResourceUsage(self->r, (M3ResourceLimit)(intptr_t)closure);
    env_unlock(self->env);
    return PyLong_FromUnsignedLongLong(used);
}

// Suspendable execution. Like gas metering, the pause points are compiled into the
// bodies, so this has to be switched on before anything compiles.
static int
Runtime_setSuspendable(m3_runtime *self, PyObject *value, void * closure)
{
    if (!value) {
        PyErr_SetString(PyExc_AttributeError, "cannot delete suspendable");
        return -1;
    }
    int enable = PyObject_IsTrue(value);
    if (enable < 0) {
        return -1;
    }
    env_lock(self->env);
    m3_SetSuspendable(self->r, enable);
    env_unlock(self->env);
    return 0;
}

static PyObject *
Runtime_getSuspendable(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    bool enabled = self->r->isSuspendable;
    env_unlock(self->env);
    return PyBool_FromLong(enabled);
}

static PyObject *
Runtime_getSuspended(m3_runtime *self, void * closure)
{
    env_lock(self->env);
    bool suspended = m3_IsSuspended(self->r);
    env_unlock(self->env);
    return PyBool_FromLong(suspended);
}

// Takes no lock: it is meant for another thread to interrupt a call that holds it, and
// all it does is raise a flag the running code polls.
static PyObject *
M3_Runtime_request_suspend(m3_runtime *self, PyObject *unused)
{
    m3_RequestSuspend(self->r);
    Py_RETURN_NONE;
}

static PyObject *
M3_Runtime_resume_unlocked(m3_runtime *self, PyObject *unused)
{
    // m3_ResumeRuntime succeeds at resuming nothing, which would hand back the
    // results of whatever ran last
    if (!m3_IsSuspended(self->r)) {
        PyErr_SetString(PyExc_RuntimeError, "nothing to resume: no call is suspended");
        return NULL;
    }
    M3Result err = m3_ResumeRuntime(self->r);
    // A resume that finishes leaves its results where the paused call would have,
    // and names that call in lastCalled - which is the only way to find it when it
    // was restored from a snapshot rather than made from here.
    return call_outcome(self->r, self->r->lastCalled, err);
}

static PyObject *
M3_Runtime_save_snapshot_unlocked(m3_runtime *self, PyObject *unused)
{
    void *bytes = NULL;
    size_t size = 0;
    M3Result err = m3_SaveSnapshotToBuffer(self->r, &bytes, &size);
    if (err) {
        return formatError(PyExc_RuntimeError, self->r, err);
    }
    PyObject *result = PyBytes_FromStringAndSize((const char *)bytes, (Py_ssize_t)size);
    m3_Free(bytes);
    return result;
}

static PyObject *
M3_Runtime_load_snapshot_unlocked(m3_runtime *self, PyObject *args)
{
    PyObject *arg;
    Py_buffer data;
    if (!PyArg_ParseTuple(args, "O!y*:load_snapshot", (PyTypeObject *)M3_Module_Type, &arg, &data)) {
        return NULL;
    }
    m3_module *module = (m3_module *)arg;
    if (module->runtime != self) {
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_RuntimeError, "module is not loaded into this runtime");
        return NULL;
    }
    // Read through in full before this returns: nothing keeps the bytes
    arena_lock();
    M3Result err = m3_LoadSnapshotFromBuffer(self->r, module->m, data.buf, (size_t)data.len);
    arena_unlock();
    PyBuffer_Release(&data);
    if (err) {
        return formatError(PyExc_RuntimeError, self->r, err);
    }
    Py_RETURN_NONE;
}

WITH_ENV_LOCK(M3_Runtime_load,             m3_runtime, self->env)
WITH_ENV_LOCK(M3_Runtime_find_function,    m3_runtime, self->env)
WITH_ENV_LOCK(M3_Runtime_resume,           m3_runtime, self->env)
WITH_ENV_LOCK(M3_Runtime_save_snapshot,    m3_runtime, self->env)
WITH_ENV_LOCK(M3_Runtime_load_snapshot,    m3_runtime, self->env)

static PyGetSetDef M3_Runtime_properties[] = {
    {"gas_limit",   (getter) Runtime_getGasLimit, (setter) Runtime_setGasLimit,
        "gas budget; setting it re-arms the runtime with a full budget, 0 disables metering", NULL},
    {"gas_used",    (getter) Runtime_getGasUsed, NULL, "gas used since gas_limit was last set", NULL},
    {"memory_limit", (getter) Runtime_getResourceLimit, (setter) Runtime_setResourceLimit,
        "cap on linear memory bytes across the runtime, 0 for none", (void *)(intptr_t)c_m3Limit_MemoryBytes},
    {"memory_used", (getter) Runtime_getResourceUsage, NULL,
        "linear memory bytes allocated across the runtime", (void *)(intptr_t)c_m3Limit_MemoryBytes},
    {"table_limit", (getter) Runtime_getResourceLimit, (setter) Runtime_setResourceLimit,
        "cap on table elements across the runtime, 0 for none", (void *)(intptr_t)c_m3Limit_TableElements},
    {"table_used",  (getter) Runtime_getResourceUsage, NULL,
        "table elements allocated across the runtime", (void *)(intptr_t)c_m3Limit_TableElements},
    {"continuation_limit", (getter) Runtime_getResourceLimit, (setter) Runtime_setResourceLimit,
        "cap on concurrently active continuation stacks, 0 for none", (void *)(intptr_t)c_m3Limit_Continuations},
    {"continuation_used", (getter) Runtime_getResourceUsage, NULL,
        "continuation stacks currently active", (void *)(intptr_t)c_m3Limit_Continuations},
    {"suspendable", (getter) Runtime_getSuspendable, (setter) Runtime_setSuspendable,
        "whether calls can be paused; set it before anything compiles", NULL},
    {"suspended",   (getter) Runtime_getSuspended, NULL, "whether a paused call is waiting for resume()", NULL},
    {NULL}  /* Sentinel */
};

static PyMethodDef M3_Runtime_methods[] = {
    {"load",            (PyCFunction)M3_Runtime_load,  METH_O,
        PyDoc_STR("load(module) -> None")},
    {"find_function", (PyCFunction)M3_Runtime_find_function,  METH_O,
        PyDoc_STR("find_function(name) -> Function")},
    {"request_suspend", (PyCFunction)M3_Runtime_request_suspend,  METH_NOARGS,
        PyDoc_STR("request_suspend() -> None")},
    {"resume",          (PyCFunction)M3_Runtime_resume,  METH_NOARGS,
        PyDoc_STR("resume() -> result, or None if it paused again")},
    {"save_snapshot",   (PyCFunction)M3_Runtime_save_snapshot,  METH_NOARGS,
        PyDoc_STR("save_snapshot() -> bytes")},
    {"load_snapshot",   (PyCFunction)M3_Runtime_load_snapshot,  METH_VARARGS,
        PyDoc_STR("load_snapshot(module, data) -> None")},
    {NULL,              NULL}           /* sentinel */
};

static PyType_Slot M3_Runtime_Type_slots[] = {
    {Py_tp_doc, "The wasm3.Runtime type"},
    {Py_tp_finalize, delRuntime},
    // {Py_tp_new, newRuntime},
    {Py_tp_methods, M3_Runtime_methods},
    {Py_tp_getset, M3_Runtime_properties},
    {0, 0}
};

static PyObject *
Module_name(m3_module *self, void * closure)
{
    return PyUnicode_FromString(m3_GetModuleName(self->m));
}

static int
Module_setGasLimit(m3_module *self, PyObject *value, void * closure)
{
    int64_t gas = PyFloat_AsDouble(value)*M3_GAS_UNITS_PER_GAS;
    env_lock(self->env);
    self->total_gas = self->current_gas = gas;
    env_unlock(self->env);
    return 0;
}

static PyObject *
Module_getGasLimit(m3_module *self, void * closure)
{
    env_lock(self->env);
    int64_t gas = self->total_gas;
    env_unlock(self->env);
    return PyFloat_FromDouble((double)gas/M3_GAS_UNITS_PER_GAS);
}

static PyObject *
Module_getGasUsed(m3_module *self, void * closure)
{
    env_lock(self->env);
    int64_t gas = self->total_gas - self->current_gas;
    env_unlock(self->env);
    return PyFloat_FromDouble((double)gas/M3_GAS_UNITS_PER_GAS);
}

static void
delModule(m3_module *self)
{
    IM3Module module = self->m;
    m3_runtime *runtime = self->runtime;

    self->m = NULL;
    self->runtime = NULL;

    if (!runtime) {
        env_lock(self->env);
        arena_lock();
        m3_FreeModule(module);
        arena_unlock();
        env_unlock(self->env);
    }

    Py_XDECREF(self->bytes);
    self->bytes = NULL;
    // Only this module's own reference: a runtime it was loaded into holds the same
    // list, and the functions in it stay callable through that module.
    Py_XDECREF(self->linked);
    self->linked = NULL;
    Py_XDECREF((PyObject *)self->env);
    self->env = NULL;
    Py_XDECREF((PyObject *)runtime);
}

m3ApiRawFunction(CallImport)
{
    PyObject *pFunc = (PyObject *)(_ctx->userdata);
    IM3Function f = _ctx->function;
    int nArgs = m3_GetArgCount(f);
    int nRets = m3_GetRetCount(f);
    PyObject *pArgs = PyTuple_New(nArgs);
    if (!pArgs) {
        m3ApiTrap("python call: args not allocated");
    }

    for (Py_ssize_t i = 0; i < nArgs; ++i) {
        PyObject *arg = get_arg_from_stack(&_sp[i+nRets], m3_GetArgType(f, i));
        if (!arg) {
            Py_DECREF(pArgs);
            m3ApiTrap(trapException);
        }
        PyTuple_SetItem(pArgs, i, arg);     // steals the reference
    }

    PyObject * pRets = PyObject_CallObject(pFunc, pArgs);
    Py_DECREF(pArgs);
    if (!pRets) m3ApiTrap(trapException);

    // Single exit from here on: a guest calling an import in a loop would otherwise
    // leak the result of every single call.
    M3Result result = m3Err_none;

    if (PyTuple_Check(pRets)) {
        if (PyTuple_Size(pRets) == nRets) {
            for (Py_ssize_t i = 0; i < nRets; ++i) {
                put_arg_on_stack(&_sp[i], m3_GetRetType(f, i), PyTuple_GetItem(pRets, i));
            }
        } else {
            result = "python call: return tuple length mismatch";
        }
    } else {
        if (nRets == 0) {
            // A value returned where none is expected is dropped, None included.
        } else if (nRets == 1) {
            if (pRets == Py_None) {
                result = "python call: should return a value";
            } else {
                put_arg_on_stack(&_sp[0], m3_GetRetType(f, 0), pRets);
            }
        } else {
            result = "python call: should return a tuple";
        }
    }

    Py_DECREF(pRets);

    // put_arg_on_stack() leaves an exception set when a returned value is not a number.
    // Trapping on it here raises it at the call that caused it, instead of leaving it
    // pending for whatever runs next.
    if (!result && PyErr_Occurred()) {
        result = trapException;
    }
    if (result) {
        m3ApiTrap(result);
    }
    m3ApiSuccess();
}

static PyObject *
M3_Module_link_function_unlocked(m3_module *self, PyObject *args)
{
    PyObject *mod_name, *func_name, *func_sig, *pFunc;
    if (PyTuple_Size(args) == 4) {
        mod_name  = PyTuple_GetItem(args, 0);
        func_name = PyTuple_GetItem(args, 1);
        func_sig  = PyTuple_GetItem(args, 2);
        pFunc     = PyTuple_GetItem(args, 3);
    } else if (PyTuple_Size(args) == 3) {
        mod_name  = PyTuple_GetItem(args, 0);
        func_name = PyTuple_GetItem(args, 1);
        func_sig  = NULL;
        pFunc     = PyTuple_GetItem(args, 2);
    } else {
        PyErr_SetString(PyExc_TypeError, "link_function takes 3 or 4 arguments");
        return NULL;
    }

    if (!PyCallable_Check(pFunc)) {
        PyErr_SetString(PyExc_TypeError, "function should be a callable object");
        return NULL;
    }
    // wasm3 holds pFunc as raw userdata, so the module - and once loaded, the runtime
    // it was loaded into - owns a reference to it for as long as it can be called.
    if (PyList_Append(self->linked, pFunc) < 0) {
        return NULL;
    }

    M3Result err = m3_LinkRawFunctionEx (self->m, as_utf8(mod_name), as_utf8(func_name),
                                         (func_sig?as_utf8(func_sig):NULL), CallImport, pFunc);
    if (err && err != m3Err_functionLookupFailed) {
        PySequence_DelItem(self->linked, PyList_Size(self->linked) - 1);
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(self->m), err);
    }

    Py_RETURN_NONE;
}

static PyObject *
M3_Module_link_global_unlocked(m3_module *self, PyObject *args)
{
    if (PyTuple_Size(args) != 3) {
        PyErr_SetString(PyExc_TypeError, "link_global takes 3 arguments");
        return NULL;
    }

    PyObject *mod_name = PyTuple_GetItem(args, 0);
    PyObject *global_name = PyTuple_GetItem(args, 1);
    PyObject *value = PyTuple_GetItem(args, 2);

    const char *mod_name_utf8 = as_utf8(mod_name);
    const char *global_name_utf8 = as_utf8(global_name);
    if (!mod_name_utf8 || !global_name_utf8) {
        return NULL;
    }

    IM3Global g = m3_FindGlobal(self->m, global_name_utf8);
    M3ValueType type = m3_GetGlobalType(g);
    if (type == c_m3Type_none) {
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(self->m), m3Err_globalLookupFailed);
    }

    M3TaggedValue tagged;
    if (set_tagged_value(&tagged, type, value) < 0) {
        return NULL;
    }

    M3Result err = m3_LinkGlobal(self->m, mod_name_utf8, global_name_utf8, &tagged);
    if (err) {
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(self->m), err);
    }

    Py_RETURN_NONE;
}

static PyObject *
M3_Module_get_global_unlocked(m3_module *self, PyObject *name)
{
    M3TaggedValue tagged;
    IM3Global g = m3_FindGlobal(self->m, as_utf8(name));
    M3Result err = m3_GetGlobal (g, &tagged);
    if (err) {
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(self->m), err);
    }
    switch (tagged.type) {
        case c_m3Type_i32:  return PyLong_FromLong(     tagged.value.i32);   break;
        case c_m3Type_i64:  return PyLong_FromLongLong( tagged.value.i64);   break;
        case c_m3Type_f32:  return PyFloat_FromDouble(  tagged.value.f32);   break;
        case c_m3Type_f64:  return PyFloat_FromDouble(  tagged.value.f64);   break;
        default:            return PyErr_Format(PyExc_TypeError, "unknown type %d", (int)tagged.type);
    }
}

static PyObject *
M3_Module_set_global_unlocked(m3_module *self, PyObject *args)
{
    if (PyTuple_Size(args) != 2) {
        PyErr_SetString(PyExc_TypeError, "set_global takes 2 arguments");
        return NULL;
    }

    PyObject *name  = PyTuple_GetItem(args, 0);
    PyObject *value = PyTuple_GetItem(args, 1);

    IM3Global g = m3_FindGlobal(self->m, as_utf8(name));

    M3TaggedValue tagged;
    if (set_tagged_value(&tagged, m3_GetGlobalType(g), value) < 0) {
        return NULL;
    }

    M3Result err = m3_SetGlobal (g, &tagged);

    if (err) {
        return formatError(PyExc_RuntimeError, m3_GetModuleRuntime(self->m), err);
    }

    Py_RETURN_NONE;
}

static PyObject *
M3_Module_get_memory_unlocked(m3_module *self, PyObject *args)
{
    PyObject *key = NULL;
    if (!PyArg_ParseTuple(args, "|O:get_memory", &key)) {
        return NULL;
    }
    // Memories are allocated when the module is instantiated
    if (!self->runtime) {
        PyErr_SetString(PyExc_RuntimeError, "module is not loaded");
        return NULL;
    }

    uint32_t index = 0;
    if (key && PyUnicode_Check(key)) {
        const char *name = as_utf8(key);
        if (!name) {
            return NULL;
        }
        if (m3_FindExportedMemory(self->m, name, &index)) {
            return PyErr_Format(PyExc_RuntimeError, "%s: no memory exported as '%s'", m3Err_unknownMemory, name);
        }
    } else {
        long long i = 0;
        if (key) {
            i = PyLong_AsLongLong(key);
            if (i == -1 && PyErr_Occurred()) {
                return NULL;
            }
        }
        if (i < 0 || i >= self->m->numMemories) {
            return PyErr_Format(PyExc_RuntimeError, "%s: module has no memory %lld", m3Err_unknownMemory, i);
        }
        index = (uint32_t)i;
    }

    m3_memory *mem = PyObject_New(m3_memory, (PyTypeObject*)M3_Memory_Type);
    if (!mem) return NULL;
    Py_INCREF((PyObject *)self);
    mem->module = self;
    mem->index = index;
    return (PyObject *)mem;
}

// A Memory holds the module and index, never the pointer: memory.grow reallocates
// linear memory, so every access looks it up afresh.
static uint8_t *
memory_data(m3_memory *self, Py_ssize_t *o_size)
{
    size_t size = 0;
    env_lock(self->module->env);
    uint8_t *data = m3_GetMemory(self->module->m, &size, self->index);
    env_unlock(self->module->env);
    *o_size = (size > PY_SSIZE_T_MAX) ? PY_SSIZE_T_MAX : (Py_ssize_t)size;
    return data;
}

static int
Memory_getbuffer(m3_memory *self, Py_buffer *view, int flags)
{
    static uint8_t empty;
    Py_ssize_t size;
    uint8_t *data = memory_data(self, &size);
    return PyBuffer_FillInfo(view, (PyObject *)self, data ? data : &empty, size, 0, flags);
}

static Py_ssize_t
Memory_length(m3_memory *self)
{
    Py_ssize_t size;
    memory_data(self, &size);
    return size;
}

// Indexing goes through a memoryview of the memory as it is right now, which gives
// ints, slices, steps and bounds checks their usual meaning. The lock is held for as
// long as the view is, so a guest running in another thread cannot grow the memory
// out from under it.
static PyObject *
Memory_subscript(m3_memory *self, PyObject *key)
{
    env_lock(self->module->env);
    PyObject *view = PyMemoryView_FromObject((PyObject *)self);
    if (!view) {
        env_unlock(self->module->env);
        return NULL;
    }
    PyObject *item = PyObject_GetItem(view, key);
    // A slice would still point into the memory: hand out a copy instead
    if (item && !PyLong_Check(item)) {
        PyObject *copy = PyBytes_FromObject(item);
        Py_DECREF(item);
        item = copy;
    }
    Py_DECREF(view);
    env_unlock(self->module->env);
    return item;
}

static int
Memory_ass_subscript(m3_memory *self, PyObject *key, PyObject *value)
{
    if (!value) {
        PyErr_SetString(PyExc_TypeError, "cannot delete memory");
        return -1;
    }
    env_lock(self->module->env);
    PyObject *view = PyMemoryView_FromObject((PyObject *)self);
    int res = view ? PyObject_SetItem(view, key, value) : -1;
    Py_XDECREF(view);
    env_unlock(self->module->env);
    return res;
}

static PyObject *
Memory_repr(m3_memory *self)
{
    return PyUnicode_FromFormat("<wasm3.Memory %u of '%s', %zd bytes>", (unsigned)self->index,
                                m3_GetModuleName(self->module->m), Memory_length(self));
}

static void
delMemory(m3_memory *self)
{
    Py_XDECREF((PyObject *)self->module);
    self->module = NULL;
}

static PyType_Slot M3_Memory_Type_slots[] = {
    {Py_tp_doc, "The wasm3.Memory type: a module's linear memory"},
    {Py_tp_finalize, delMemory},
    {Py_tp_repr, Memory_repr},
    {Py_mp_length, Memory_length},
    {Py_mp_subscript, Memory_subscript},
    {Py_mp_ass_subscript, Memory_ass_subscript},
    {Py_bf_getbuffer, Memory_getbuffer},
    {0, 0}
};

static PyGetSetDef M3_Module_properties[] = {
    {"name",        (getter) Module_name, NULL, "module name", NULL},
    {"gasLimit",    (getter) Module_getGasLimit, (setter) Module_setGasLimit, "gas limit for metered modules", NULL},
    {"gasUsed",     (getter) Module_getGasUsed, NULL, "gas used", NULL},
    {0},
};

WITH_ENV_LOCK(M3_Module_link_function,     m3_module, self->env)
WITH_ENV_LOCK(M3_Module_link_global,       m3_module, self->env)
WITH_ENV_LOCK(M3_Module_get_global,        m3_module, self->env)
WITH_ENV_LOCK(M3_Module_set_global,        m3_module, self->env)
WITH_ENV_LOCK(M3_Module_get_memory,        m3_module, self->env)

static PyMethodDef M3_Module_methods[] = {
    {"link_function", (PyCFunction)M3_Module_link_function,  METH_VARARGS,
        PyDoc_STR("link_function(module, name, signature, function)")},

    {"link_global", (PyCFunction)M3_Module_link_global,  METH_VARARGS,
        PyDoc_STR("link_global(module, name, value)")},

    {"get_global", (PyCFunction)M3_Module_get_global,  METH_O,
        PyDoc_STR("get_global(name) -> value")},

    {"set_global", (PyCFunction)M3_Module_set_global,  METH_VARARGS,
        PyDoc_STR("set_global(name, value)")},

    {"get_memory", (PyCFunction)M3_Module_get_memory,  METH_VARARGS,
        PyDoc_STR("get_memory(index_or_export_name=0) -> Memory")},

    {NULL,              NULL}           /* sentinel */
};

static PyType_Slot M3_Module_Type_slots[] = {
    {Py_tp_doc, "The wasm3.Module type"},
    {Py_tp_finalize, delModule},
    // {Py_tp_new, newModule},
    {Py_tp_methods, M3_Module_methods},
    {Py_tp_getset, M3_Module_properties},
    {0, 0}
};

static PyObject *
get_results(IM3Runtime runtime, IM3Function f)
{
    int nRets = m3_GetRetCount(f);
    if (nRets <= 0) {
        Py_RETURN_NONE;
    }

    if (nRets > MAX_ARGS) {
        PyErr_SetString(PyExc_RuntimeError, "too many rets");
        return NULL;
    }

    uint64_t    valbuff[MAX_ARGS];
    const void* valptrs[MAX_ARGS];
    memset(valbuff, 0, sizeof(valbuff));
    memset(valptrs, 0, sizeof(valptrs));

    for (int i = 0; i < nRets; i++) {
        valptrs[i] = &valbuff[i];
    }
    M3Result err = m3_GetResults (f, nRets, valptrs);
    if (err) {
        return formatError(PyExc_RuntimeError, runtime, err);
    }

    if (nRets == 1) {
        return get_arg_from_stack(valptrs[0], m3_GetRetType(f, 0));
    } else {
        PyObject *ret = PyTuple_New(nRets);
        if (ret) {
            Py_ssize_t i;
            for (i = 0; i < nRets; ++i) {
                PyObject *val = get_arg_from_stack(valptrs[i], m3_GetRetType(f, i));
                PyTuple_SetItem(ret, i, val);
            }
        }
        return ret;
    }
}

static
void print_backtrace(IM3Runtime runtime)
{
    IM3BacktraceInfo info = m3_GetBacktrace(runtime);
    if (!info) {
        return;
    }

    fprintf(stderr, "==== wasm backtrace:");

    int frameCount = 0;
    IM3BacktraceFrame curr = info->frames;
    while (curr)
    {
        fprintf(stderr, "\n  %d: 0x%06x - %s!%s",
                           frameCount, curr->moduleOffset,
                           m3_GetModuleName (m3_GetFunctionModule(curr->function)),
                           m3_GetFunctionName (curr->function)
               );
        curr = curr->next;
        frameCount++;
    }
    if (info->lastFrame == M3_BACKTRACE_TRUNCATED) {
        fprintf(stderr, "\n  (truncated)");
    }
    fprintf(stderr, "\n");
}

// What a call or a resume hands back to Python: the results, None for one that paused
// (Runtime.suspended tells the two apart), or the exception.
static PyObject *
call_outcome(IM3Runtime runtime, IM3Function f, M3Result err)
{
    if (err == m3Err_continuationSuspended) {
        Py_RETURN_NONE;
    } else if (err == trapException) {
        return NULL;
    } else if (err) {
        print_backtrace(runtime);
        return formatError(PyExc_RuntimeError, runtime, err);
    }
    return get_results(runtime, f);
}

static PyObject *
M3_Function_call_argv_unlocked(m3_function *func, PyObject *args)
{
    Py_ssize_t size = PyTuple_Size(args);
    const char* argv[MAX_ARGS];
    for(Py_ssize_t i = 0; i< size;++i) {
        PyObject *arg = PyTuple_GetItem(args, i);
        if (!PyUnicode_Check(arg)) {
            PyErr_SetString(PyExc_RuntimeError, "all arguments should be strings");
            return NULL;
        }
        argv[i] = as_utf8(arg);
    }
    M3Result err = m3_CallArgv(func->f, size, argv);
    return call_outcome(func->r, func->f, err);
}

static PyObject*
M3_Function_call_unlocked(m3_function *self, PyObject *args)
{
    IM3Function f = self->f;

    int nArgs = m3_GetArgCount(f);

    if (nArgs > MAX_ARGS) {
        PyErr_SetString(PyExc_RuntimeError, "too many args");
        return NULL;
    }

    uint64_t    valbuff[MAX_ARGS];
    const void* valptrs[MAX_ARGS];
    memset(valbuff, 0, sizeof(valbuff));   // was sizeof(args): a pointer's size
    memset(valptrs, 0, sizeof(valptrs));

    for (int i = 0; i < nArgs; i++) {
        uint64_t* s = &valbuff[i];
        valptrs[i] = s;
        put_arg_on_stack(s, m3_GetArgType(f, i), PyTuple_GetItem(args, i));
    }

    M3Result err = m3_Call (f, nArgs, valptrs);
    return call_outcome(self->r, f, err);
}

WITH_ENV_LOCK(M3_Function_call_argv,       m3_function, self->runtime->env)
WITH_ENV_LOCK(M3_Function_call,            m3_function, self->runtime->env)

static PyObject*
M3_Function_tp_call(m3_function *self, PyObject *args, PyObject *kwargs)
{
    return M3_Function_call(self, args);
}

static PyObject*
Function_name(m3_function *self, void * closure)
{
    return PyUnicode_FromString(m3_GetFunctionName(self->f));
}

static PyObject*
Function_num_args(m3_function *self, void * closure)
{
    return PyLong_FromLong(m3_GetArgCount(self->f));
}

static PyObject*
Function_num_rets(m3_function *self, void * closure)
{
    return PyLong_FromLong(m3_GetRetCount(self->f));
}

static PyObject*
Function_arg_types(m3_function *self, void * closure)
{
    Py_ssize_t nArgs = m3_GetArgCount(self->f);
    PyObject *ret = PyTuple_New(nArgs);
    if (ret) {
        Py_ssize_t i;
        for (i = 0; i < nArgs; ++i) {
            PyTuple_SetItem(ret, i, PyLong_FromLong(m3_GetArgType(self->f, i)));
        }
    }
    return ret;
}

static PyObject*
Function_ret_types(m3_function *self, void * closure)
{
    Py_ssize_t nRets = m3_GetRetCount(self->f);
    PyObject *ret = PyTuple_New(nRets);
    if (ret) {
        Py_ssize_t i;
        for (i = 0; i < nRets; ++i) {
            PyTuple_SetItem(ret, i, PyLong_FromLong(m3_GetRetType(self->f, i)));
        }
    }
    return ret;
}

static void
delFunction(m3_function *self)
{
    self->f = NULL;
    self->r = NULL;
    Py_XDECREF((PyObject *)self->runtime);
    self->runtime = NULL;
}

static PyGetSetDef M3_Function_properties[] = {
    {"name", (getter) Function_name, NULL, "function name", NULL },
    {"num_args", (getter) Function_num_args, NULL, "number of args", NULL },
    {"num_rets", (getter) Function_num_rets, NULL, "number of rets", NULL },
    {"arg_types", (getter) Function_arg_types, NULL, "types of args", NULL },
    {"ret_types", (getter) Function_ret_types, NULL, "types of rets", NULL },
    {NULL}  /* Sentinel */
};

static PyMethodDef M3_Function_methods[] = {
    {"call_argv", (PyCFunction)M3_Function_call_argv,  METH_VARARGS,
        PyDoc_STR("call_argv(args...) -> result")},
    {NULL, NULL}           /* sentinel */
};

static PyType_Slot M3_Function_Type_slots[] = {
    {Py_tp_doc, "The wasm3.Function type"},
    {Py_tp_finalize, delFunction},
    // {Py_tp_new, newFunction},
    {Py_tp_call, M3_Function_tp_call},
    {Py_tp_methods, M3_Function_methods},
    {Py_tp_getset, M3_Function_properties},
    {0, 0}
};

static PyType_Spec M3_Environment_Type_spec = {
    "wasm3.Environment",
    sizeof(m3_environment),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    M3_Environment_Type_slots
};

static PyType_Spec M3_Runtime_Type_spec = {
    "wasm3.Runtime",
    sizeof(m3_runtime),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    M3_Runtime_Type_slots
};

static PyType_Spec M3_Module_Type_spec = {
    "wasm3.Module",
    sizeof(m3_module),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    M3_Module_Type_slots
};

static PyType_Spec M3_Function_Type_spec = {
    "wasm3.Function",
    sizeof(m3_function),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    M3_Function_Type_slots
};

static PyType_Spec M3_Memory_Type_spec = {
    "wasm3.Memory",
    sizeof(m3_memory),
    0,
    // Only Module.get_memory() makes one: a bare Memory would have no module
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    M3_Memory_Type_slots
};

static int
m3_modexec(PyObject *m)
{
    M3_Environment_Type = PyType_FromSpec(&M3_Environment_Type_spec);
    if (M3_Environment_Type == NULL)
        goto fail;
    M3_Runtime_Type = PyType_FromSpec(&M3_Runtime_Type_spec);
    if (M3_Runtime_Type == NULL)
        goto fail;
    M3_Module_Type = PyType_FromSpec(&M3_Module_Type_spec);
    if (M3_Module_Type == NULL)
        goto fail;
    M3_Function_Type = PyType_FromSpec(&M3_Function_Type_spec);
    if (M3_Function_Type == NULL)
        goto fail;
    M3_Memory_Type = PyType_FromSpec(&M3_Memory_Type_spec);
    if (M3_Memory_Type == NULL)
        goto fail;
    if (PyModule_AddStringMacro(m, M3_VERSION) < 0)
        goto fail;
    // AddObjectRef, not AddObject: keeps the static M3_*_Type pointers owners.
    if (PyModule_AddObjectRef(m, "Environment", M3_Environment_Type) < 0)
        goto fail;
    if (PyModule_AddObjectRef(m, "Runtime", M3_Runtime_Type) < 0)
        goto fail;
    if (PyModule_AddObjectRef(m, "Module", M3_Module_Type) < 0)
        goto fail;
    if (PyModule_AddObjectRef(m, "Function", M3_Function_Type) < 0)
        goto fail;
    if (PyModule_AddObjectRef(m, "Memory", M3_Memory_Type) < 0)
        goto fail;
    return 0;
 fail:
    // m is borrowed (Py_mod_exec lends it), so no decref here.
    return -1;
}

static PyModuleDef_Slot m3_slots[] = {
    {Py_mod_exec, m3_modexec},
#ifdef Py_GIL_DISABLED
    // Everything that reaches into wasm3 takes its environment's lock - see env_lock()
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL}
};

PyDoc_STRVAR(m3_doc,
"wasm3 python bindings");

static struct PyModuleDef m3module = {
    PyModuleDef_HEAD_INIT,
    "wasm3._wasm3",
    m3_doc,
    0,
    0, // methods
    m3_slots,
    NULL,
    NULL,
    NULL
};

PyMODINIT_FUNC
PyInit__wasm3(void)
{
    return PyModuleDef_Init(&m3module);
}
