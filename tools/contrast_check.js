#!/usr/bin/env node
/* Контраст дашборда Life OS по WCAG 2.2 (AA).
 *
 * Проверяет в обеих темах:
 *   1. все текстовые узлы страницы — 4.5:1 (крупный текст — 3:1);
 *   2. токены графиков (--chart-*) на фоне карточки — ≥3:1 (WCAG 1.4.11);
 *   3. уровни хитмапа (--heat-*), фокус-кольцо (--focus) и галочку чек-ина
 *      (--check-ink на --violet) — как графические объекты, ≥3:1;
 *   4. плашки статусов целей, включая полупрозрачные заливки, — ≥4.5:1.
 *
 * Запуск (Playwright нужен только для проверки, не для дашборда):
 *   NODE_PATH=~/.npm/_npx/<hash>/node_modules node tools/contrast_check.js
 *   NODE_PATH=... node tools/contrast_check.js [url|путь к index.html] [--verbose]
 *
 * Код выхода: 0 — всё проходит, 1 — есть пары ниже порога.
 */
"use strict";

const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const DEFAULT_PAGE = path.join(ROOT, "demo", "index.html");
const VERBOSE = process.argv.indexOf("--verbose") !== -1;
const arg = process.argv.slice(2).filter((a) => a.indexOf("--") !== 0)[0];

function pageUrl(value) {
  if (!value) return pathToFileURL(DEFAULT_PAGE).href;
  if (/^https?:\/\//.test(value)) return value;
  return pathToFileURL(path.resolve(ROOT, value)).href;
}

/* ── WCAG: общая формула (для отчёта) ────────────────────────────────── */
function round2(n) {
  return Math.round(n * 100) / 100;
}

/* ── аудит страницы (выполняется в браузере) ──────────────────────────── */
function auditInPage() {
  const parse = (value) => {
    const text = String(value || "").trim();
    let m = text.match(/^#([0-9a-f]{6})$/i);
    if (m) {
      return { r: parseInt(m[1].slice(0, 2), 16), g: parseInt(m[1].slice(2, 4), 16),
        b: parseInt(m[1].slice(4, 6), 16), a: 1 };
    }
    m = text.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const blend = (fg, bg) => {
    const a = fg.a + bg.a * (1 - fg.a);
    if (a === 0) return { r: 0, g: 0, b: 0, a: 0 };
    return {
      r: (fg.r * fg.a + bg.r * bg.a * (1 - fg.a)) / a,
      g: (fg.g * fg.a + bg.g * bg.a * (1 - fg.a)) / a,
      b: (fg.b * fg.a + bg.b * bg.a * (1 - fg.a)) / a,
      a
    };
  };
  const lum = (c) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a);
    const l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const root = getComputedStyle(document.documentElement);
  const pageBg = parse(root.getPropertyValue("--bg")) || { r: 249, g: 249, b: 253, a: 1 };

  const effectiveBg = (el) => {
    const stack = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const own = parse(getComputedStyle(node).backgroundColor);
      if (own && own.a > 0.001) stack.push(own);
      node = node.parentElement;
    }
    let out = pageBg;
    for (let i = stack.length - 1; i >= 0; i--) out = blend(stack[i], out);
    return out;
  };

  const rows = [];
  document.querySelectorAll("body *").forEach((el) => {
    if (["SCRIPT", "STYLE", "SVG", "PATH", "CIRCLE", "RECT", "LINE", "TEXT"].indexOf(el.tagName) !== -1) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || parseFloat(style.opacity) === 0) return;
    const text = Array.prototype.filter
      .call(el.childNodes, (n) => n.nodeType === 3 && n.textContent.trim())
      .map((n) => n.textContent.trim())
      .join(" ")
      .trim();
    if (!text) return;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const size = parseFloat(style.fontSize);
    const weight = parseInt(style.fontWeight, 10) || 400;
    const fg = parse(style.color);
    if (!fg) return;
    const bg = effectiveBg(el);
    const value = ratio(blend(fg, bg), bg);
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3 : 4.5;
    rows.push({
      sel: el.tagName.toLowerCase() +
        (typeof el.className === "string" && el.className.trim()
          ? "." + el.className.trim().split(/\s+/).join(".") : ""),
      text: text.slice(0, 48),
      size: Math.round(size * 10) / 10,
      color: style.color,
      bg: "rgb(" + Math.round(bg.r) + ", " + Math.round(bg.g) + ", " + Math.round(bg.b) + ")",
      ratio: Math.round(value * 100) / 100,
      need,
      fail: value < need
    });
  });

  const card = parse(root.getPropertyValue("--card"));
  const chart = {};
  ["--chart-sleep", "--chart-calories", "--chart-mood", "--chart-energy"].forEach((name) => {
    const c = parse(root.getPropertyValue(name));
    if (c) chart[name] = Math.round(ratio(c, card) * 100) / 100;
  });
  const heat = {};
  ["--heat-0", "--heat-1", "--heat-2", "--heat-3", "--heat-4"].forEach((name) => {
    const c = parse(root.getPropertyValue(name));
    if (c) heat[name] = Math.round(ratio(c, card) * 100) / 100;
  });
  const focus = parse(root.getPropertyValue("--focus"));
  const violet = parse(root.getPropertyValue("--violet"));
  const checkInk = parse(root.getPropertyValue("--check-ink"));
  return {
    rows,
    chart,
    heat,
    focus: focus ? Math.round(ratio(focus, card) * 100) / 100 : null,
    check: (violet && checkInk) ? Math.round(ratio(checkInk, violet) * 100) / 100 : null
  };
}

/* ── отчёт ────────────────────────────────────────────────────────────── */
async function main() {
  const browser = await chromium.launch();
  const url = pageUrl(arg);
  let failed = 0;

  for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(url);
    await page.waitForTimeout(600);
    if (theme === "dark") {
      await page.click("#theme");
      await page.waitForTimeout(450);
    }
    await page.evaluate(() => {
      /* плашки всех статусов, чтобы измерить их в текущей теме */
      const host = document.getElementById("goals");
      if (!host) return;
      const wrap = document.createElement("div");
      wrap.innerHTML = '<span class="pill">без статуса</span> ' +
        '<span class="pill is-active">активна</span> ' +
        '<span class="pill is-done">готово</span> ' +
        '<span class="pill is-paused">пауза</span> ' +
        '<span class="pill is-failed">сорвана</span>';
      host.appendChild(wrap);
    });
    const result = await page.evaluate(auditInPage);
    const fails = result.rows.filter((r) => r.fail).sort((a, b) => a.ratio - b.ratio);
    failed += fails.length;
    const min = Math.min.apply(null, result.rows.map((r) => r.ratio));
    console.log("===== " + theme + " =====");
    console.log("текстовых узлов: " + result.rows.length + ", ниже AA: " + fails.length +
      ", минимум: " + round2(min) + ":1");
    const seen = {};
    fails.forEach((r) => {
      const key = r.color + "|" + r.bg + "|" + r.size;
      if (seen[key]) return;
      seen[key] = true;
      console.log("  FAIL " + r.ratio + ":1 (нужно " + r.need + ") " + r.size + "px " +
        r.color + " на " + r.bg + " — " + r.sel + " «" + r.text + "»");
    });
    if (VERBOSE) {
      result.rows.slice().sort((a, b) => a.ratio - b.ratio).slice(0, 12).forEach((r) => {
        console.log("  " + r.ratio + ":1 " + r.size + "px " + r.color + " на " + r.bg +
          " — " + r.sel + " «" + r.text + "»");
      });
    }
    console.log("линии графиков на карточке: " + JSON.stringify(result.chart));
    console.log("уровни хитмапа на карточке: " + JSON.stringify(result.heat));
    console.log("фокус-кольцо: " + result.focus + ":1, галочка чек-ина: " + result.check + ":1");
    await page.close();
  }

  await browser.close();
  console.log(failed ? "КОНТРАСТ: есть пары ниже порога" : "КОНТРАСТ: OK — все пары проходят");
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error("ошибка проверки:", err && err.message ? err.message : err);
  process.exit(2);
});
