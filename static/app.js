(() => {
  const $ = (id) => document.getElementById(id);
  const screens = { login: $("screen-login"), guide: $("screen-guide"), annotate: $("screen-annotate") };

  let META = null;
  let TOKENS = [];
  let user = null;
  let current = null; // current sentence
  let chips = []; // [{kind: 'sign'|'fs'|'sign_not_found', value}]
  let suggestions = [];
  let activeIdx = -1;
  let pendingSpecial = null; // 'fs' | 'sign_not_found'

  const SPECIAL = [
    { kind: "fs", label: "fs-", hint: "Fingerspell a word", cls: "special-fs" },
    { kind: "sign_not_found", label: "SIGN_NOT_FOUND", hint: "No sign in dictionary (suggest one)", cls: "special-missing" },
  ];

  const api = async (url, opts) => {
    const r = await fetch(url, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  };

  function show(name) {
    Object.entries(screens).forEach(([k, el]) => el.classList.toggle("hidden", k !== name));
  }

  // ---------- Login ----------
  async function initLogin() {
    const users = await api("/api/users");
    const sel = $("existing-users");
    sel.innerHTML = '<option value="">-- choose --</option>';
    users.forEach((u) => {
      const o = document.createElement("option");
      o.value = u.id;
      o.textContent = `${u.name} (${u.association})`;
      o.dataset.user = JSON.stringify(u);
      sel.appendChild(o);
    });
    const assoc = $("new-association");
    assoc.innerHTML = "";
    META.associations.forEach((a) => {
      const o = document.createElement("option");
      o.value = a; o.textContent = a;
      assoc.appendChild(o);
    });
    const saved = localStorage.getItem("gsl_user");
    if (saved) {
      const u = JSON.parse(saved);
      if (users.some((x) => x.id === u.id)) sel.value = u.id;
    }
  }

  function setUser(u) {
    user = u;
    localStorage.setItem("gsl_user", JSON.stringify(u));
    $("user-label").textContent = `${u.name} · ${u.association}`;
    $("user-bar").classList.remove("hidden");
    show("guide");
  }

  $("btn-login").onclick = () => {
    const sel = $("existing-users");
    const opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) return;
    setUser(JSON.parse(opt.dataset.user));
  };

  $("btn-create").onclick = async () => {
    $("create-error").textContent = "";
    try {
      const u = await api("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $("new-name").value, association: $("new-association").value }),
      });
      setUser(u);
    } catch (e) {
      $("create-error").textContent = e.message;
    }
  };

  $("switch-user").onclick = async () => {
    user = null;
    localStorage.removeItem("gsl_user");
    $("user-bar").classList.add("hidden");
    await initLogin();
    show("login");
  };

  $("btn-start").onclick = () => { show("annotate"); loadNext(); };

  // ---------- Annotation ----------
  async function loadNext() {
    const data = await api(`/api/next?annotator_id=${encodeURIComponent(user.id)}`);
    const p = data.progress;
    $("progress-bar").style.width = `${p.required ? (100 * p.done) / p.required : 0}%`;
    $("progress-text").textContent = `${p.done} / ${p.required} annotations collected overall · you have submitted ${p.mine}`;
    if (!data.sentence) {
      $("annotate-card").classList.add("hidden");
      $("done-card").classList.remove("hidden");
      if (data.all_complete) {
        $("done-title").textContent = "All sentences have been annotated!";
        $("done-text").textContent = `Every one of the ${META.total_sentences} sentences has received its required annotations. Thank you!`;
        alert("All sentences have been annotated. Thank you!");
      } else {
        $("done-title").textContent = "Nothing left for you to annotate";
        $("done-text").textContent = "You have annotated every sentence available to you. The remaining sentences need annotations from other annotators.";
        alert("You have annotated every sentence available to you.");
      }
      return;
    }
    current = data.sentence;
    $("annotate-card").classList.remove("hidden");
    $("done-card").classList.add("hidden");
    $("sentence-text").textContent = current.english_sentence;
    $("sentence-meta").innerHTML = `<span>ID: <b>${current.id}</b></span><span>${esc(current.category)}</span><span>${esc(current.sentence_type)}</span><span>${current.word_count} words · ${esc(current.length_band)}</span>` +
      (current.required > 1 ? `<span title="This sentence is annotated by ${current.required} different annotators">multi-annotator sentence</span>` : "");
    resetForm();
    $("token-input").focus();
  }

  function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function resetForm() {
    chips = [];
    renderChips();
    $("token-input").value = "";
    hideSuggestions();
    cancelSpecial();
    $("nmm").value = "";
    $("notes").value = "";
    document.querySelectorAll('input[name=confidence]').forEach((r) => (r.checked = false));
    $("submit-error").textContent = "";
    $("btn-submit").disabled = false;
  }

  function chipText(c) {
    if (c.kind === "fs") return `fs-"${c.value}"`;
    if (c.kind === "sign_not_found") return c.value ? `SIGN_NOT_FOUND(${c.value.toUpperCase()})` : "SIGN_NOT_FOUND";
    return c.value;
  }

  function renderChips() {
    const box = $("chips");
    box.innerHTML = "";
    chips.forEach((c, i) => {
      const el = document.createElement("span");
      el.className = "chip" + (c.kind === "fs" ? " chip-fs" : c.kind === "sign_not_found" ? " chip-missing" : "");
      el.title = c.kind === "fs" ? "Fingerspelled" : c.kind === "sign_not_found" ? "Sign not found in dictionary" : "Dictionary sign";
      const label = document.createElement("span");
      label.textContent = chipText(c);
      el.appendChild(label);
      const left = document.createElement("button"); left.textContent = "‹"; left.title = "Move left"; left.disabled = i === 0;
      left.onclick = (e) => { e.stopPropagation(); [chips[i - 1], chips[i]] = [chips[i], chips[i - 1]]; renderChips(); };
      const right = document.createElement("button"); right.textContent = "›"; right.title = "Move right"; right.disabled = i === chips.length - 1;
      right.onclick = (e) => { e.stopPropagation(); [chips[i + 1], chips[i]] = [chips[i], chips[i + 1]]; renderChips(); };
      const x = document.createElement("button"); x.textContent = "×"; x.title = "Remove";
      x.onclick = (e) => { e.stopPropagation(); chips.splice(i, 1); renderChips(); $("token-input").focus(); };
      el.append(left, right, x);
      box.appendChild(el);
    });
    $("gloss-preview").textContent = chips.map(chipText).join(" ");
  }

  // ---------- Autocomplete ----------
  function computeSuggestions(q) {
    q = q.trim().toUpperCase();
    const out = [];
    if (!q) return out;
    const starts = [], contains = [];
    for (const t of TOKENS) {
      if (t.token === q) { starts.unshift(t); continue; }
      if (t.token.startsWith(q)) starts.push(t);
      else if (t.token.includes(q)) contains.push(t);
    }
    starts.slice(0, 12).forEach((t) => out.push({ kind: "sign", token: t.token, cat: t.category }));
    contains.slice(0, Math.max(0, 12 - out.length)).forEach((t) => out.push({ kind: "sign", token: t.token, cat: t.category }));
    SPECIAL.forEach((s) => {
      if (isSpecialQuery(s.kind, q)) out.unshift({ kind: s.kind, token: s.label, cat: s.hint, cls: s.cls });
      else out.push({ kind: s.kind, token: s.label, cat: s.hint, cls: s.cls });
    });
    return out;
  }

  function isSpecialQuery(kind, q) {
    q = q.trim().toUpperCase();
    if (!q) return false;
    if (kind === "fs") return q === "F" || q === "FS" || q.startsWith("FS-");
    return "SIGN_NOT_FOUND".startsWith(q) || "SIGN NOT FOUND".startsWith(q) || "NOT FOUND".startsWith(q) || q === "?";
  }

  function renderSuggestions(q) {
    const ul = $("suggestions");
    ul.innerHTML = "";
    if (!suggestions.length) { ul.classList.add("hidden"); return; }
    const qq = q.trim().toUpperCase();
    suggestions.forEach((s, i) => {
      const li = document.createElement("li");
      li.className = (s.cls || "") + (i === activeIdx ? " active" : "");
      const tok = document.createElement("span"); tok.className = "tok";
      const idx = qq ? s.token.toUpperCase().indexOf(qq) : -1;
      if (idx >= 0 && s.kind === "sign") {
        tok.innerHTML = esc(s.token.slice(0, idx)) + "<mark>" + esc(s.token.slice(idx, idx + qq.length)) + "</mark>" + esc(s.token.slice(idx + qq.length));
      } else tok.textContent = s.token;
      const cat = document.createElement("span"); cat.className = "cat"; cat.textContent = s.cat;
      li.append(tok, cat);
      li.onmousedown = (e) => { e.preventDefault(); choose(s); };
      ul.appendChild(li);
    });
    ul.classList.remove("hidden");
    const act = ul.children[activeIdx];
    if (act) act.scrollIntoView({ block: "nearest" });
  }

  function hideSuggestions() { suggestions = []; activeIdx = -1; $("suggestions").classList.add("hidden"); }

  function choose(s) {
    const input = $("token-input");
    const typed = input.value.trim();
    if (s.kind === "sign") {
      chips.push({ kind: "sign", value: s.token });
      renderChips();
      input.value = "";
      hideSuggestions();
      input.focus();
      return;
    }
    // Special tokens need extra text
    let prefill = "";
    if (s.kind === "fs" && /^fs-./i.test(typed)) prefill = typed.slice(3);
    else if (!isSpecialQuery(s.kind, typed)) prefill = typed;
    input.value = "";
    hideSuggestions();
    startSpecial(s.kind, prefill);
  }

  function startSpecial(kind, prefill) {
    pendingSpecial = kind;
    $("special-entry").classList.remove("hidden");
    $("special-label").textContent = kind === "fs" ? 'Fingerspell (fs-"..."):' : "SIGN_NOT_FOUND — suggested sign (optional):";
    const inp = $("special-input");
    inp.value = prefill || "";
    inp.placeholder = kind === "fs" ? "letters to fingerspell" : "your suggested sign, e.g. SUBTITLE";
    inp.focus();
  }

  function commitSpecial() {
    if (!pendingSpecial) return;
    const val = $("special-input").value.trim();
    if (pendingSpecial === "fs") {
      if (!val) { $("special-input").focus(); return; }
      chips.push({ kind: "fs", value: val });
    } else {
      chips.push({ kind: "sign_not_found", value: val.toUpperCase() });
    }
    renderChips();
    cancelSpecial();
    $("token-input").focus();
  }

  function cancelSpecial() {
    pendingSpecial = null;
    $("special-entry").classList.add("hidden");
    $("special-input").value = "";
  }

  $("special-ok").onclick = commitSpecial;
  $("special-cancel").onclick = () => { cancelSpecial(); $("token-input").focus(); };
  $("special-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commitSpecial(); }
    if (e.key === "Escape") { cancelSpecial(); $("token-input").focus(); }
  });

  const input = $("token-input");
  input.addEventListener("input", () => {
    suggestions = computeSuggestions(input.value);
    activeIdx = suggestions.length ? 0 : -1;
    renderSuggestions(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && suggestions.length) { e.preventDefault(); activeIdx = (activeIdx + 1) % suggestions.length; renderSuggestions(input.value); }
    else if (e.key === "ArrowUp" && suggestions.length) { e.preventDefault(); activeIdx = (activeIdx - 1 + suggestions.length) % suggestions.length; renderSuggestions(input.value); }
    else if (e.key === "Enter" || e.key === "Tab") {
      if (suggestions.length && activeIdx >= 0) { e.preventDefault(); choose(suggestions[activeIdx]); }
      else if (input.value.trim()) { e.preventDefault(); }
    }
    else if (e.key === "Escape") hideSuggestions();
    else if (e.key === "Backspace" && !input.value && chips.length) { chips.pop(); renderChips(); }
  });
  input.addEventListener("blur", () => setTimeout(hideSuggestions, 150));
  $("token-box").addEventListener("click", (e) => { if (e.target === $("token-box") || e.target === $("chips")) input.focus(); });

  // ---------- Submit ----------
  $("btn-submit").onclick = async () => {
    const err = $("submit-error");
    err.textContent = "";
    if (pendingSpecial) { err.textContent = "Finish or cancel the pending fs-/SIGN_NOT_FOUND entry first."; return; }
    if (!chips.length) { err.textContent = "Add at least one GSL token."; return; }
    const conf = document.querySelector("input[name=confidence]:checked");
    if (!conf) { err.textContent = "Please select your confidence (1-3)."; return; }
    $("btn-submit").disabled = true;
    try {
      await api("/api/annotations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: current.id,
          annotator_id: user.id,
          tokens: chips,
          confidence: Number(conf.value),
          non_manual_markers: $("nmm").value,
          notes: $("notes").value,
        }),
      });
      await loadNext();
    } catch (e) {
      err.textContent = e.message;
      $("btn-submit").disabled = false;
    }
  };

  // ---------- Boot ----------
  (async () => {
    META = await api("/api/meta");
    TOKENS = await api("/api/tokens");
    await initLogin();
    show("login");
  })();
})();
