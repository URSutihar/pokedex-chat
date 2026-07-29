/* ---------------------------------------------------------------------------
   Conversation store — localStorage only, no server, no database.

   Layout:
     pokedex-convs      → [{id, title, updated, model, messages:[{role,content}]}]
     pokedex-active     → id of the open conversation

   Everything lives in the browser, so it survives reloads, stays private to this
   machine, and costs the server nothing. The trade is the usual one: it is
   per-browser and bounded by the ~5 MB localStorage quota, so old conversations
   are evicted oldest-first when we run out of room rather than throwing.
   --------------------------------------------------------------------------- */

const CONV_KEY = "pokedex-convs";
const ACTIVE_KEY = "pokedex-active";
const MAX_CONVS = 60;

function uid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

export const store = {
  all() {
    try {
      const raw = JSON.parse(localStorage.getItem(CONV_KEY) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch {
      return [];
    }
  },

  save(convs) {
    convs.sort((a, b) => b.updated - a.updated);
    let list = convs.slice(0, MAX_CONVS);
    for (;;) {
      try {
        localStorage.setItem(CONV_KEY, JSON.stringify(list));
        return list;
      } catch (e) {
        // QuotaExceededError: drop the oldest and try again rather than losing
        // the conversation the user is actually in.
        if (list.length <= 1) {
          console.error("conversation store full", e);
          return list;
        }
        list = list.slice(0, list.length - 1);
      }
    }
  },

  get(id) {
    return this.all().find(c => c.id === id) || null;
  },

  create(model) {
    const conv = { id: uid(), title: "New chat", updated: Date.now(), model, messages: [] };
    const list = this.all();
    list.unshift(conv);
    this.save(list);
    this.setActive(conv.id);
    return conv;
  },

  update(id, patch) {
    const list = this.all();
    const i = list.findIndex(c => c.id === id);
    if (i === -1) return null;
    list[i] = { ...list[i], ...patch, updated: Date.now() };
    this.save(list);
    return list[i];
  },

  remove(id) {
    const list = this.all().filter(c => c.id !== id);
    this.save(list);
    if (this.activeId() === id) localStorage.removeItem(ACTIVE_KEY);
    return list;
  },

  clearAll() {
    localStorage.removeItem(CONV_KEY);
    localStorage.removeItem(ACTIVE_KEY);
  },

  activeId() {
    return localStorage.getItem(ACTIVE_KEY) || "";
  },

  setActive(id) {
    localStorage.setItem(ACTIVE_KEY, id);
  },

  /** First user message, trimmed — good enough, and costs no tokens. */
  titleFor(text) {
    const t = text.replace(/\s+/g, " ").trim();
    return t.length > 52 ? t.slice(0, 51) + "…" : t || "New chat";
  },

  exportAll() {
    return JSON.stringify(
      { exported: new Date().toISOString(), conversations: this.all() },
      null,
      2
    );
  },
};
