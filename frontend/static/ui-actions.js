/* The interface's control wiring under its Content-Security-Policy
 * (script-src 'self': no inline event-handler attribute runs). A control says
 * what it does declaratively -- data-dp-click, data-dp-change, data-dp-input or
 * data-dp-keydown name an action -- and the module that renders it owns one
 * delegated listener per event type on its own container, with a frozen table
 * of that module's actions (DPActions.delegate). An action is never looked up
 * by its name anywhere else (never as a global), so a name inside a file name
 * or other text cannot become a call; an unknown name goes to the console only.
 *
 * The walk is the inline handlers' own bubbling: from the event's target up
 * to the container, the first element naming an action for this event type
 * runs it (with that element as `this` was, and the event unchanged) and ends
 * the walk; an element whose data-dp-stop lists the type ends it without an
 * action and stops the event there, as its `event.stopPropagation()` did. A
 * walk that ran or stopped is the event's one: a container further out never
 * walks it again. */
(function () {
  'use strict';

  const walked = new WeakSet();

  function stops(node, type) {
    const listed = node.getAttribute('data-dp-stop');
    return listed !== null && listed.split(/\s+/).includes(type);
  }

  function delegate(container, type, table) {
    if (!container) return;
    const actions = Object.freeze(Object.assign(Object.create(null), table));
    const attribute = 'data-dp-' + type;
    container.addEventListener(type, function (event) {
      if (walked.has(event)) return;
      let node = event.target instanceof Element ? event.target : event.target?.parentElement;
      for (; node; node = node.parentElement) {
        const name = node.getAttribute(attribute);
        if (name !== null) {
          walked.add(event);
          const action = actions[name];
          if (typeof action === 'function') action(node, event);
          else console.warn('DPActions.unknown', type, name);
          return;
        }
        if (stops(node, type)) {
          walked.add(event);
          event.stopPropagation();
          return;
        }
        if (node === container) return;
      }
    });
  }

  window.DPActions = Object.freeze({delegate});
})();
