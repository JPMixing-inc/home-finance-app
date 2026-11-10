// Small progressive enhancements; every page works without this file except inline category/note edits.

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) { e.preventDefault(); return; }
  // Slow actions (Claude): show progress and stop double submits
  const busy = e.target.dataset.busy;
  if (busy) {
    const btn = e.target.querySelector("button");
    if (btn.disabled) { e.preventDefault(); return; }
    setTimeout(() => { btn.disabled = true; btn.textContent = busy; }, 0);
  }
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-confirm]");
  if (btn && !confirm(btn.dataset.confirm)) e.preventDefault();
});

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("signed out");
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

let toastTimer;
function toast(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2500);
}

document.querySelectorAll(".cat-select").forEach((select) => {
  select.addEventListener("change", async () => {
    const row = select.closest("tr");
    const categoryId = select.value;
    try {
      const res = await postJSON(`/api/transactions/${select.dataset.tx}`, { category_id: categoryId });
      row.classList.toggle("uncat", !categoryId);
      toast("Saved");
      if (categoryId && res.similar > 0) {
        const name = select.options[select.selectedIndex].text;
        const pattern = prompt(
          `${res.similar} other uncategorized transaction(s) look similar.\n` +
          `Create a rule so descriptions containing this text become "${name}" (now and on future imports)?\n` +
          `Edit the text if you like, or Cancel to skip.`,
          res.pattern,
        );
        if (pattern && pattern.trim()) {
          const r = await postJSON("/api/rules", { pattern: pattern.trim(), category_id: categoryId });
          toast(`Rule added: ${r.changed} more categorized`);
          if (r.changed) setTimeout(() => location.reload(), 900);
        }
      }
    } catch (err) {
      toast("Couldn't save. Check your connection.");
    }
  });
});

document.querySelectorAll("input.note").forEach((input) => {
  input.addEventListener("change", async () => {
    try {
      await postJSON(`/api/transactions/${input.dataset.tx}`, { notes: input.value });
      toast("Note saved");
    } catch (err) {
      toast("Couldn't save the note.");
    }
  });
});

// Light / dark switch (dark is the default; the choice is remembered in this browser)
document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) { /* private mode: just this page view */ }
  document.dispatchEvent(new Event("themechange"));
});

// Close the "More" menu when clicking anywhere else
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.nav-more[open]").forEach((menu) => {
    if (!menu.contains(e.target)) menu.removeAttribute("open");
  });
});
