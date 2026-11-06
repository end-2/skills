// Serialized into Chrome's isolated realm; keep this function self-contained.
export function collectDocument(state, maxNodes) {
  if (state.document !== document) throw new Error('Document changed');
  const nodes = [];
  const seen = new Set();
  const elements = new Map();
  let truncated = false;
  let visited = 0;
  let activeElement = document.activeElement;
  while (activeElement?.shadowRoot?.activeElement) activeElement = activeElement.shadowRoot.activeElement;
  const idFor = element => {
    let id = state.ids.get(element);
    if (!id) { id = String(state.nextId++); state.ids.set(element, id); }
    return id;
  };
  const labelFor = element => {
    const labelled = element.getAttribute('aria-labelledby');
    if (labelled) {
      const label = labelled.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
      if (label) return label;
    }
    return element.getAttribute('aria-label') ||
      [...(element.labels || [])].map(label => label.textContent).join(' ').trim() ||
      element.getAttribute('alt') || element.getAttribute('title') ||
      element.getAttribute('placeholder') || '';
  };
  const roleFor = element => {
    if (element.getAttribute('role')) return element.getAttribute('role');
    const tag = element.tagName.toLowerCase();
    if (tag === 'a' && element.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      if (['button', 'submit', 'reset', 'image'].includes(element.type)) return 'button';
      if (['checkbox', 'radio'].includes(element.type)) return element.type;
      if (element.type === 'range') return 'slider';
      if (element.type === 'number') return 'spinbutton';
      return 'textbox';
    }
    return {textarea: 'textbox', select: 'combobox', option: 'option', img: 'image',
      h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
      p: 'paragraph', li: 'listitem', ul: 'list', ol: 'list', table: 'table', tr: 'row',
      th: 'columnheader', td: 'cell', iframe: 'iframe', main: 'main', nav: 'navigation',
      form: 'form'}[tag] || (element.isContentEditable ? 'textbox' : 'generic');
  };
  function visit(element, parentId) {
    if (!(element instanceof Element) || seen.has(element)) return;
    seen.add(element);
    if (++visited > 20000 || nodes.length >= maxNodes) { truncated = true; return; }
    const tag = element.tagName.toLowerCase();
    if (['script', 'style', 'head', 'noscript', 'template'].includes(tag)) return;
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' || element.hidden) return;
    const rect = element.getBoundingClientRect();
    const canFocus = element.tabIndex >= 0 || element.isContentEditable;
    const nativeClick = ['button', 'a', 'input', 'select', 'option', 'summary'].includes(tag);
    const role = roleFor(element);
    const semanticClick = ['button', 'link', 'checkbox', 'radio', 'menuitem', 'tab', 'option', 'switch', 'treeitem'].includes(role);
    const click = nativeClick || semanticClick || element.hasAttribute('onclick') || style.cursor === 'pointer';
    const editable = (tag === 'textarea' || (tag === 'input' &&
      ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].includes(element.type)) || element.isContentEditable) && !element.readOnly;
    const directText = [...element.childNodes].filter(child => child.nodeType === Node.TEXT_NODE).map(child => child.textContent).join(' ').replace(/\s+/g, ' ').trim();
    const name = labelFor(element) || (click ? element.textContent?.replace(/\s+/g, ' ').trim() : directText) || '';
    const keep = element === document.body || click || canFocus || editable || role !== 'generic' || name;
    let nextParent = parentId;
    if (keep) {
      const id = idFor(element);
      const disabled = element.matches(':disabled') || element.getAttribute('aria-disabled') === 'true' || !!element.closest('[inert]');
      const node = {local_id: id, parent_id: parentId, role, title: name, tag,
        enabled: !disabled, focused: element === activeElement,
        actions: [], settable: [], children: [],
        frame: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        scroll: {x: element.scrollLeft, y: element.scrollTop}};
      if (!disabled && rect.width > 0 && rect.height > 0) {
        if (click) node.actions.push('click');
        node.actions.push('hover');
        if (canFocus) node.settable.push('focused');
        if (editable) node.settable.push('value');
      }
      if (tag === 'input' && element.type === 'password') node.secure = true;
      else if ('value' in element && ['input', 'textarea', 'select'].includes(tag)) node.value = String(element.value);
      else if (element.isContentEditable) node.value = element.textContent || '';
      if ('checked' in element && ['checkbox', 'radio'].includes(element.type)) node.checked = element.checked;
      if (element.getAttribute('aria-expanded') !== null) node.expanded = element.getAttribute('aria-expanded');
      if (element.getAttribute('aria-selected') !== null) node.selected = element.getAttribute('aria-selected');
      if (tag === 'a') node.href = element.href;
      nodes.push(node);
      elements.set(id, element);
      nextParent = id;
    }
    if (nodes.length >= maxNodes) { if (element.children.length || element.shadowRoot) truncated = true; return; }
    for (const child of element.children) visit(child, nextParent);
    if (element.shadowRoot) for (const child of element.shadowRoot.children) visit(child, nextParent);
    if (tag.includes('-') && !element.shadowRoot && !element.children.length && !name) truncated = true;
  }
  if (document.body) visit(document.body, null);
  else if (document.documentElement) visit(document.documentElement, null);
  state.elements = elements;
  return {nodes, truncated, url: document.URL, title: document.title, has_focus: document.hasFocus(),
    scroll: {x: scrollX, y: scrollY}, ready_state: document.readyState,
    viewport: {width: innerWidth, height: innerHeight}};
}
