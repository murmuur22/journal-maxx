const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('static/recording.js', 'utf8');

function page({dirty = false, busy = false, fetchResult, fetchError, actionAttribute = '/journal/early/save/'} = {}) {
  const events = {}, pageEvents = {}, controls = [{disabled: false, value: 'unsaved words'}];
  const notice = {hidden: true}, earlier = {hidden: false};
  let reloaded = false, redirect, feedback, interval, destination;
  const form = {
    action: {toString: () => '[object RadioNodeList]'}, elements: controls,
    addEventListener(name, callback) {events[name] = callback;},
    getAttribute(name) {return name === 'action' ? actionAttribute : busy ? 'true' : null;},
    setAttribute() {}, removeAttribute() {},
    querySelector() {return feedback;}, querySelectorAll() {return [];},
    append(item) {feedback = item;},
  };
  const panel = {dataset: {boundary: '2000-01-01T19:00:00Z'}, querySelector() {return notice;}};
  const document = {
    querySelector(selector) {return selector === '[data-recording-window]' ? panel : form;},
    querySelectorAll() {return [earlier];},
    addEventListener() {}, createElement() {return {dataset: {}, setAttribute() {}};},
  };
  const window = {location: {href: 'http://127.0.0.1:8000/journal/', reload() {reloaded = true;}, assign(url) {redirect = url;}},
    addEventListener(name, callback) {pageEvents[name] = callback;}};
  vm.runInNewContext(source, {window, document, Date, setInterval(callback) {interval = callback;},
    FormData: class {append() {}}, fetch: async (url) => {
      destination = url;
      if (fetchError) throw new Error(fetchError);
      return {ok: fetchResult.ok, json: async () => fetchResult.body};
    }, Error});
  if (dirty) events.input();
  return {controls, events, notice, earlier, check: () => interval(),
    get destination() {return destination;}, get reloaded() {return reloaded;}, get redirect() {return redirect;}, get feedback() {return feedback;}};
}

test('a clean page transitions automatically and hides earlier content', () => {
  const p = page(); p.check();
  assert.equal(p.reloaded, true);
  assert.equal(p.earlier.hidden, true);
});

test('a dirty or uploading page preserves input at the cutoff', () => {
  for (const options of [{dirty: true}, {busy: true}]) {
    const p = page(options); p.check();
    assert.equal(p.reloaded, false);
    assert.equal(p.notice.hidden, false);
    assert.equal(p.earlier.hidden, true);
    assert.equal(p.controls[0].value, 'unsaved words');
  }
});

test('a rejected or unconfirmed save leaves input editable', async () => {
  for (const options of [{fetchResult: {ok: false, body: {error: 'Window closed'}}}, {fetchError: 'Connection lost'}]) {
    const p = page(options);
    await p.events.submit({preventDefault() {}, defaultPrevented: false});
    assert.equal(p.controls[0].disabled, false);
    assert.equal(p.controls[0].value, 'unsaved words');
    assert.ok(p.feedback.textContent);
    assert.equal(p.redirect, undefined);
  }
});

test('confirmed text-only saves navigate and uploads keep their shared handler', async () => {
  const p = page({fetchResult: {ok: true, body: {redirect: '/journal/'}}});
  await p.events.submit({preventDefault() {}, defaultPrevented: false});
  assert.equal(p.redirect, '/journal/');
  const upload = page();
  await upload.events.submit({defaultPrevented: true});
  assert.equal(upload.controls[0].disabled, false);
  assert.equal(upload.redirect, undefined);
});


test('named action buttons cannot mask the text submission URL', async () => {
  for (const actionAttribute of ['/journal/', null]) {
    const p = page({actionAttribute, fetchResult: {ok: true, body: {redirect: '/journal/cards/saved/'}}});
    await p.events.submit({preventDefault() {}, defaultPrevented: false, submitter: {name: 'action', value: 'submit'}});
    assert.equal(p.destination, actionAttribute || 'http://127.0.0.1:8000/journal/');
    assert.equal(p.redirect, '/journal/cards/saved/');
  }
});

test('attachment submissions use the URL attribute even with action buttons', () => {
  for (const actionAttribute of ['/journal/', null]) {
    const node = () => ({
      events: {}, classList: {add() {}, remove() {}, toggle() {}},
      addEventListener(name, callback) {this.events[name] = callback;},
      append() {}, replaceChildren() {}, remove() {}, setAttribute() {},
      focus() {}, scrollIntoView() {}, cloneNode: () => node(),
    });
    const input = node(); input.files = [{name: 'note.txt', size: 4}];
    const label = node(); label.querySelector = () => input;
    const form = node(); form.elements = []; form.action = {toString: () => '[object RadioNodeList]'};
    form.querySelectorAll = () => []; form.hasAttribute = () => false;
    form.getAttribute = () => actionAttribute;
    const picker = node(); picker.closest = () => form;
    picker.dataset = {fileLimit: '1024', cardLimit: '2048', storedBytes: '0'};
    const parts = new Map([['label', label]]);
    picker.querySelector = selector => {
      if (!parts.has(selector)) parts.set(selector, node());
      return parts.get(selector);
    };
    let destination, sent = false;
    const window = {location: {href: 'http://127.0.0.1:8000/journal/'}, addEventListener() {}};
    const document = {querySelectorAll: selector => selector === '[data-attachment-picker]' ? [picker] : [], createElement: node};
    vm.runInNewContext(readFileSync('static/attachments.js', 'utf8'), {
      document, window,
      FormData: class {append() {}},
      DataTransfer: class {files = []; items = {add: file => this.files.push(file)};},
      XMLHttpRequest: class {
        upload = node();
        open(method, url) {assert.equal(method, 'POST'); destination = url;}
        setRequestHeader() {} addEventListener() {} send() {sent = true;}
      },
    });
    input.events.change();
    const event = {defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}, submitter: {name: 'action', value: 'submit'}};
    form.events.submit(event);
    assert.equal(sent, true);
    assert.equal(destination, actionAttribute || 'http://127.0.0.1:8000/journal/');
  }
});
