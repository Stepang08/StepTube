/* StepTube courses: finds lecture series from titles alone.

   A title such as "Lecture 7: Backprop", "Занятие 12. Графы",
   "Ботай со мной #149 | Борис Трушин" or "[03] Tokenization | LLM course"
   carries a number. Videos of one channel whose titles share the text around
   that number form a course, ordered by the number. Groups that are too small,
   or whose numbers mostly repeat (e.g. "Problem 1" across many years), are
   dropped. */
(() => {
  "use strict";
  const KW = "lecture|lec|lesson|part|episode|ep|tutorial|class|chapter|session|element|module|unit|" +
             "лекция|занятие|урок|семинар|часть|выпуск|серия|դաս";
  const NUM = new RegExp(`(?:^|[\\s\\[(|:,.—–-])(?:(${KW})\\.?\\s*(?:#|№|no\\.?\\s*)?|(#|№)\\s?)(\\d{1,3})(?:[.:](\\d{1,2}))?(?![\\d%])`, "iu");
  const LEAD = /^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)]\s)/u;
  const clean = (s) => s.toLowerCase().replace(/\d+/g, "").replace(/[\s\-—–:,.|()[\]"«»]+/g, " ").trim();

  function parse(title) {
    const segs = title.split(/\s*\|\s*/);
    const lead = LEAD.exec(title);
    if (lead && segs.length > 1) {
      const rest = segs.slice(1).join(" | ");
      return { n: +(lead[1] || lead[2]), sub: 0, fine: clean(rest), coarse: clean(rest), label: rest };
    }
    for (const seg of segs) {
      const m = NUM.exec(seg);
      if (!m) continue;
      const before = seg.slice(0, m.index + m[0].length).replace(/[\d.:]+$/, "");
      const prefix = clean(before.replace(/[#№]\s*$/, ""));
      const bare = !prefix || prefix === (m[1] || "").toLowerCase();
      const last = segs.length > 1 && segs[segs.length - 1] !== seg ? segs[segs.length - 1] : "";
      const label = before.replace(/[\s#№:.\-—–([]+$/, "").trim();
      return {
        n: +m[3], sub: +(m[4] || 0),
        coarse: prefix || "#",
        fine: bare && last ? `${prefix || "#"} ${clean(last)}` : (prefix || "#"),
        label: bare && last ? `${last}${label ? " · " + label : ""}` : label.replace(/^[\[(]+/, ""),
      };
    }
    return null;
  }

  function build(videos, chName) {
    const groups = new Map(), parsed = new Map();
    const chOf = (v) => v.ch || v.ccid || v.chn || "";
    const add = (key, v) => { let g = groups.get(key); if (!g) groups.set(key, (g = [])); g.push(v); };
    for (const v of videos) {
      const p = parse(v.t); if (!p || !chOf(v)) continue;
      parsed.set(v.id, p);
      add(`${chOf(v)}|${p.fine}`, v);
    }
    // Videos whose fine group is too small fall back to the coarser key.
    for (const [key, g] of [...groups]) {
      if (g.length >= 4) continue;
      groups.delete(key);
      for (const v of g) { const p = parsed.get(v.id); if (p.coarse !== p.fine) add(`${chOf(v)}|${p.coarse}`, v); }
    }
    const out = [];
    for (const [key, g] of groups) {
      if (g.length < 4) continue;
      const nums = new Set(g.map((v) => { const p = parsed.get(v.id); return p.n * 100 + p.sub; }));
      if (nums.size < 4 || nums.size / g.length < 0.6) continue;
      g.sort((a, b) => { const x = parsed.get(a.id), y = parsed.get(b.id); return x.n - y.n || x.sub - y.sub || (a.p || "").localeCompare(b.p || ""); });
      const labels = {}; let label = "", best = 0;
      for (const v of g) { const l = parsed.get(v.id).label; labels[l] = (labels[l] || 0) + 1; if (labels[l] > best) { best = labels[l]; label = l; } }
      out.push({ key, title: label || `${chName(g[0])}: numbered series`, ch: g[0].ch || "", chn: chName(g[0]), tp: g[0].tp, ids: g.map((v) => v.id) });
    }
    return out;
  }

  window.StepCourses = { build, parse };
})();
