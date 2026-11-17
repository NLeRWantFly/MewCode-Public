import {VIEW_ERROR} from './api.js';
import {alignWorkflowView} from './workflow-summary.js?v=20261117-exploration';

// A content-addressed view cache, never a reducer for raw execution events.
export function createStore({api, now = () => performance.now(), delay = setTimeout,
  cancelDelay = clearTimeout, frame = requestAnimationFrame, cancelFrame = cancelAnimationFrame} = {}) {
  let state = {scope: {conversation_id: '', project: ''}, selection: {}, view: null, tasks: [], loading: false, error: ''};
  const subscribers = new Set(), reads = [];
  let generation = 0, timer = null, scheduledFrame = null, controller = null;
  let inflight = null, pending = false, needTasks = false, disposed = false;
  let tasksInflight = null, tasksController = null, tasksPending = false;
  let activityTimer = null, hostView = null;

  function scheduleActivity() {
    if(disposed||activityTimer!==null||hostView?.task?.mode!=='workflow')return;
    if(!['running','blocked','needs_input','paused','failed','unknown'].includes(hostView.task.execution_status))return;
    activityTimer=delay(()=>{activityTimer=null;refresh();scheduleActivity();},hostView.task.execution_status==='running'?1500:10000);
    activityTimer?.unref?.();
  }

  function emit() {
    if (disposed || scheduledFrame !== null) return;
    scheduledFrame = frame(() => {
      scheduledFrame = null;
      for (const listener of subscribers) listener(state);
    });
  }

  function accept(view, owner = generation) {
    if (disposed || owner !== generation) return false;
    if (view && view.task?.conversation_id && view.task.conversation_id !== state.scope.conversation_id) return false;
    const previous = state.view?.task, next = view?.task;
    if (previous?.id === next?.id && previous?.run_id === next?.run_id
        && Number.isFinite(previous?.kernel_revision) && Number.isFinite(next?.kernel_revision)
        && next.kernel_revision < previous.kernel_revision) return false;
    hostView=view;
    view=alignWorkflowView(view);
    const tasks = view?.task ? state.tasks.map(row => row.id === view.task.id && row.run_id === view.task.run_id
      ? {...row, ...view.task} : row) : state.tasks;
    const changed = state.view?.version !== view?.version || state.loading || !!state.error
      || JSON.stringify(tasks) !== JSON.stringify(state.tasks);
    state = {...state, tasks, view, loading: false, error: ''};
    scheduleActivity();
    if (changed) emit();
    return changed;
  }

  function schedule() {
    if (disposed || !pending || timer !== null || inflight || !state.scope.conversation_id) return;
    const at = now();
    while (reads.length && at - reads[0] >= 1000) reads.shift();
    const spacing = reads.length ? Math.max(0, reads.at(-1) + 250 - at) : 0;
    const budget = reads.length >= 4 ? Math.max(0, reads[0] + 1000 - at) : 0;
    timer = delay(() => { timer = null; void read(); }, Math.max(spacing, budget));
  }

  function discoverTasks(scope, owner) {
    if (tasksInflight) { tasksPending = true; return; }
    const abort = tasksController = new AbortController();
    const work = Promise.resolve().then(() => api.listTasks(scope, abort.signal));
    tasksInflight = work;
    void work.then(list => {
      if (owner !== generation || disposed) return;
      const current = state.view?.task;
      const rows = (list?.tasks || []).map(row => row.id === current?.id && row.run_id === current?.run_id
        ? {...row, ...current} : row);
      if (current && !rows.some(row => row.id === current.id && row.run_id === current.run_id)) rows.unshift(current);
      if (JSON.stringify(rows) !== JSON.stringify(state.tasks)) {state = {...state, tasks: rows}; emit();}
    }).catch(() => { /* Current view remains usable if history discovery fails. */ }).finally(() => {
      if (tasksInflight !== work) return;
      tasksInflight = tasksController = null;
      if (tasksPending && !disposed) {tasksPending = false; refresh({tasks: true});}
    });
  }

  async function read() {
    if (disposed || !pending || !state.scope.conversation_id) return;
    const owner = generation, scope = {...state.scope}, selection = {...state.selection};
    const tasks = needTasks;
    pending = needTasks = false;
    const abort = controller = new AbortController();
    reads.push(now());
    if (!state.view && !state.loading) { state = {...state, loading: true}; emit(); }
    // Historical task discovery must not hold back the current run's view.
    if (tasks) discoverTasks(scope, owner);
    const work = api.readView(scope, selection, abort.signal);
    inflight = work;
    try {
      const view = await work;
      if (owner !== generation || disposed) return;
      accept(view, owner);
    } catch (error) {
      if (owner === generation && !disposed && error.name !== 'AbortError') {
        state = {...state, loading: false, error: VIEW_ERROR}; emit();
      }
    } finally {
      if (inflight === work) inflight = null;
      if (controller === abort) controller = null;
      schedule();
    }
  }

  function refresh({tasks = false} = {}) {
    pending = true;
    needTasks ||= tasks;
    schedule();
  }

  function reset(next) {
    generation += 1;
    controller?.abort(); controller = null; inflight = null;
    tasksController?.abort(); tasksController = tasksInflight = null; tasksPending = false;
    if (timer !== null) cancelDelay(timer);
    timer = null; pending = needTasks = false;
    if(activityTimer!==null)cancelDelay(activityTimer);
    activityTimer=null;hostView=null;
    state = {...state, ...next, view: null, loading: false, error: ''};
    emit();
    refresh({tasks: true});
  }

  return {
    get state() { return state; },
    get generation() { return generation; },
    subscribe(listener) { subscribers.add(listener); return () => subscribers.delete(listener); },
    accept,
    refresh,
    setScope(scope) {
      const next = {conversation_id: scope.conversation_id || '', project: scope.project || ''};
      if (JSON.stringify(next) === JSON.stringify(state.scope)) return false;
      reset({scope: next, selection: {}, tasks: []}); return true;
    },
    selectTask(task) {
      const selection = {task_id: task.id, run_id: task.run_id || ''};
      if (JSON.stringify(selection) === JSON.stringify(state.selection)) return;
      reset({selection});
    },
    changed(notice) {
      if (notice.conversation_id !== state.scope.conversation_id) return;
      const known = state.tasks.some(task => task.id === notice.task_id && task.run_id === notice.run_id);
      if (state.view?.version === notice.version && known) return;
      if (state.selection.task_id && notice.task_id !== state.selection.task_id && known) return;
      refresh({tasks: !known});
    },
    reconnect(scope) {
      this.setScope(scope);
      refresh({tasks: true});
    },
    dispose() {
      disposed = true; generation += 1;
      controller?.abort();
      tasksController?.abort();
      if (timer !== null) cancelDelay(timer);
      if (activityTimer !== null) cancelDelay(activityTimer);
      if (scheduledFrame !== null) cancelFrame(scheduledFrame);
      subscribers.clear();
    },
  };
}
