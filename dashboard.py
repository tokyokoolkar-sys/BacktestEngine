"""
dashboard.py — DCA Bot Instrumentation Panel.

Usage:
    python dashboard.py [--port 5000]

Open http://localhost:5000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

try:
    from flask import Flask, jsonify, redirect, render_template_string, request, url_for
    from functools import wraps
except ImportError:
    print("Install flask: pip install flask")
    sys.exit(1)

_HERE = Path(__file__).parent

# Load .env so the dashboard AND the bot.py/trade.py subprocesses it launches
# inherit real Binance credentials / control token without the parent shell
# having exported them. Children also load .env independently on their own.
# override=True: .env is canonical — a stale/wrong-type key in the parent shell
# (e.g. an Ed25519 key) must not override the correct HMAC key in .env.
try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env", override=True)
except ImportError:
    pass

logger = logging.getLogger("dashboard")
app = Flask(__name__)
_cache = {}
_cache_lock = Lock()

# ──────────────────────────────────────────────────────────────────────────────
# LANDING PAGE
# ──────────────────────────────────────────────────────────────────────────────
LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCA Engine — 1h Scalp · Binance USD-M Futures</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ── Z-INDEX SCALE ── */
:root {
  --z-base:    0;
  --z-canvas:  10;
  --z-content: 20;
  --z-nav:     30;
  --z-overlay: 40;
}

/* ── OKLCH COLOR TOKENS ── */
:root {
  /* Backgrounds — committed dark, cool blue tint */
  --bg-void:    oklch(0.14 0.012 248);   /* near-black canvas */
  --bg-surface: oklch(0.18 0.014 248);   /* cards / panels */
  --bg-raised:  oklch(0.21 0.014 248);   /* hover / elevated */
  --bg-border:  oklch(0.25 0.014 248);   /* separator lines */

  /* Ink */
  --ink-primary:   oklch(0.94 0.008 248);  /* near-white, ≥4.5:1 on void */
  --ink-secondary: oklch(0.72 0.015 248);
  --ink-muted:     oklch(0.52 0.015 248);
  --ink-ghost:     oklch(0.38 0.012 248);

  /* Semantic — profit green, loss coral, accent teal */
  --profit:     oklch(0.76 0.18 162);    /* teal-green */
  --profit-dim: oklch(0.76 0.18 162 / 0.12);
  --profit-glow:oklch(0.76 0.18 162 / 0.28);
  --loss:       oklch(0.68 0.22 22);     /* coral-red */
  --loss-dim:   oklch(0.68 0.22 22 / 0.12);
  --accent:     oklch(0.72 0.17 212);    /* electric teal-blue */
  --accent-dim: oklch(0.72 0.17 212 / 0.12);
  --amber:      oklch(0.78 0.17 75);

  /* Typography */
  --font-body: 'Manrope', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Mono', monospace;

  /* Motion */
  --ease-expo: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-quart: cubic-bezier(0.25, 1, 0.5, 1);
}

/* ── RESET ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 15px; scroll-behavior: smooth; }

body {
  font-family: var(--font-body);
  background: var(--bg-void);
  color: var(--ink-primary);
  min-height: 100vh;
  overflow-x: hidden;
  line-height: 1.65;
}

/* ── GLOBAL NAV ── */
.nav {
  position: fixed; top: 0; left: 0; right: 0;
  z-index: var(--z-nav);
  display: flex; align-items: center; gap: 32px;
  padding: 0 clamp(16px, 5vw, 56px);
  height: 56px;
  background: oklch(0.14 0.012 248 / 0.88);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--bg-border);
}
.nav-brand {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--ink-primary);
  letter-spacing: 0.04em;
  text-decoration: none;
  flex-shrink: 0;
}
.nav-brand span { color: var(--accent); }
.nav-spacer { flex: 1; }
.nav-chip {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  font-weight: 500;
  color: var(--ink-muted);
  letter-spacing: 0.06em;
  display: flex; align-items: center; gap: 6px;
}
.nav-chip-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--profit);
  box-shadow: 0 0 6px var(--profit);
  animation: pulse-dot 2.4s ease-in-out infinite;
}
@keyframes pulse-dot {
  0%, 100% { opacity: 1; box-shadow: 0 0 6px var(--profit); }
  50%       { opacity: 0.6; box-shadow: 0 0 12px var(--profit); }
}
.nav-link {
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 500;
  color: var(--ink-secondary);
  text-decoration: none;
  letter-spacing: 0.04em;
  padding: 6px 14px;
  border: 1px solid var(--bg-border);
  border-radius: 4px;
  transition: color 0.18s, border-color 0.18s, background 0.18s;
}
.nav-link:hover {
  color: var(--ink-primary);
  border-color: var(--accent);
  background: var(--accent-dim);
}

/* ── HERO ── */
.hero {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: clamp(88px, 12vh, 140px) clamp(16px, 6vw, 80px) clamp(48px, 8vh, 80px);
  overflow: hidden;
}

/* Seed-grid canvas motif — sits behind content */
#seed-canvas {
  position: absolute;
  inset: 0;
  width: 100%; height: 100%;
  z-index: var(--z-canvas);
  opacity: 0.45;
  pointer-events: none;
}

.hero-inner {
  position: relative;
  z-index: var(--z-content);
  max-width: 960px;
}

/* Terminal session tag */
.session-tag {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  font-weight: 500;
  color: var(--ink-muted);
  letter-spacing: 0.1em;
  margin-bottom: 28px;
  display: flex; align-items: center; gap: 10px;
}
.session-tag-id {
  color: var(--accent);
}

/* Hero heading */
.hero-heading {
  font-size: clamp(2.4rem, 6.5vw, 5.6rem);
  font-weight: 800;
  line-height: 1.04;
  letter-spacing: -0.045em;
  text-wrap: balance;
  color: var(--ink-primary);
  margin-bottom: 28px;
}
.hero-heading-dim { color: var(--ink-ghost); }

/* Hero body */
.hero-body {
  font-size: clamp(0.95rem, 1.8vw, 1.1rem);
  font-weight: 500;
  color: var(--ink-secondary);
  line-height: 1.7;
  max-width: 62ch;
  margin-bottom: 44px;
}

/* CTA row */
.hero-actions {
  display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  margin-bottom: 64px;
}
.btn-primary {
  display: inline-flex; align-items: center; gap: 8px;
  font-family: var(--font-mono);
  font-size: 0.8rem; font-weight: 600;
  color: var(--bg-void);
  background: var(--profit);
  border: none; border-radius: 4px;
  padding: 12px 24px;
  text-decoration: none;
  letter-spacing: 0.04em;
  cursor: pointer;
  transition: background 0.18s, box-shadow 0.18s, transform 0.12s;
}
.btn-primary:hover {
  background: oklch(0.80 0.18 162);
  box-shadow: 0 0 24px var(--profit-glow);
  transform: translateY(-1px);
}
.btn-secondary {
  font-family: var(--font-mono);
  font-size: 0.78rem; font-weight: 500;
  color: var(--ink-secondary);
  text-decoration: none;
  letter-spacing: 0.06em;
  display: inline-flex; align-items: center; gap: 6px;
  transition: color 0.18s;
}
.btn-secondary:hover { color: var(--ink-primary); }
.btn-secondary-arrow { color: var(--accent); }

/* Live ticker strip */
.live-strip {
  display: flex; flex-wrap: wrap; gap: 10px;
}
.live-chip {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  font-weight: 500;
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 10px;
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 3px;
  color: var(--ink-muted);
  letter-spacing: 0.06em;
}
.live-chip-name { color: var(--ink-secondary); }
.live-chip-tp { color: var(--profit); }

/* ── HERO SPARKLINE PANEL ── */
.hero-visual {
  position: relative;
  z-index: var(--z-content);
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 6px;
  padding: 16px 20px 12px;
  margin-top: 48px;
  overflow: hidden;
}
.hero-visual::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent) 30%, var(--profit) 70%, transparent);
  opacity: 0.6;
}
.hv-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  margin-bottom: 12px;
}
.hv-label {
  font-family: var(--font-mono);
  font-size: 0.62rem;
  font-weight: 500;
  color: var(--ink-muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.hv-meta {
  display: flex; gap: 20px;
}
.hv-stat { text-align: right; }
.hv-stat-label {
  font-family: var(--font-mono);
  font-size: 0.58rem;
  color: var(--ink-ghost);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.hv-stat-value {
  font-family: var(--font-mono);
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--ink-primary);
}
.hv-stat-value.profit { color: var(--profit); }
.canvas-wrap {
  position: relative; width: 100%; height: 120px;
}
.canvas-wrap canvas { width: 100%; height: 100%; display: block; }
.canvas-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono); font-size: 0.75rem;
  color: var(--ink-ghost);
  letter-spacing: 0.06em;
}

/* ── STATS ROW ── */
.stats-section {
  border-top: 1px solid var(--bg-border);
  border-bottom: 1px solid var(--bg-border);
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0;
  max-width: 1200px;
  margin: 0 auto;
}
.stat-cell {
  padding: clamp(24px, 4vw, 40px) clamp(16px, 4vw, 40px);
  border-right: 1px solid var(--bg-border);
}
.stat-cell:last-child { border-right: none; }
.stat-cell-label {
  font-family: var(--font-mono);
  font-size: 0.62rem; font-weight: 500;
  color: var(--ink-muted);
  letter-spacing: 0.1em; text-transform: uppercase;
  margin-bottom: 8px;
}
.stat-cell-value {
  font-family: var(--font-mono);
  font-size: clamp(1.4rem, 3vw, 2rem);
  font-weight: 600;
  color: var(--ink-primary);
  line-height: 1;
}
.stat-cell-value.profit { color: var(--profit); }
.stat-cell-sub {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  color: var(--ink-muted);
  margin-top: 4px;
}

/* ── SPECS SECTION ── */
.specs-section {
  max-width: 1200px;
  margin: 0 auto;
  padding: clamp(40px, 6vh, 72px) clamp(16px, 5vw, 56px);
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--bg-border);
}
.spec-block {
  background: var(--bg-void);
  padding: 32px 36px;
}
.spec-block-tag {
  font-family: var(--font-mono);
  font-size: 0.6rem; font-weight: 600;
  color: var(--accent);
  letter-spacing: 0.14em; text-transform: uppercase;
  margin-bottom: 20px;
}
.spec-table {
  width: 100%; border-collapse: collapse;
}
.spec-table tr { border-bottom: 1px solid var(--bg-border); }
.spec-table tr:last-child { border-bottom: none; }
.spec-table td {
  padding: 9px 0;
  font-family: var(--font-mono);
  font-size: 0.73rem;
  vertical-align: top;
}
.spec-table td:first-child {
  color: var(--ink-muted);
  width: 46%;
  padding-right: 12px;
}
.spec-table td:last-child {
  color: var(--ink-primary);
  font-weight: 500;
}
.spec-table .val-profit { color: var(--profit); }
.spec-table .val-accent { color: var(--accent); }

/* ── SYMBOL TABLE SECTION ── */
.symbols-section {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 clamp(16px, 5vw, 56px) clamp(40px, 6vh, 72px);
}
.section-heading {
  font-family: var(--font-mono);
  font-size: 0.62rem; font-weight: 600;
  color: var(--ink-ghost);
  letter-spacing: 0.12em; text-transform: uppercase;
  margin-bottom: 20px;
  padding-top: clamp(28px, 4vh, 48px);
  border-top: 1px solid var(--bg-border);
}
.sym-table-wrap { overflow-x: auto; }
.sym-table {
  width: 100%; border-collapse: collapse;
  font-family: var(--font-mono); font-size: 0.72rem;
}
.sym-table th {
  text-align: left; padding: 8px 12px 8px 0;
  font-family: var(--font-mono); font-size: 0.6rem; font-weight: 500;
  color: var(--ink-ghost); letter-spacing: 0.1em; text-transform: uppercase;
  border-bottom: 1px solid var(--bg-border);
}
.sym-table th:first-child { padding-left: 0; }
.sym-table td {
  padding: 9px 12px 9px 0;
  border-bottom: 1px solid oklch(0.25 0.014 248 / 0.5);
  color: var(--ink-secondary);
}
.sym-table td:first-child { color: var(--ink-primary); font-weight: 600; }
.sym-table .col-wr { color: var(--profit); }
.sym-table .col-net { color: var(--profit); }
.sym-table .col-neg { color: var(--loss); }
.sym-live-badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 0.58rem; font-weight: 600;
  padding: 2px 7px;
  background: var(--profit-dim);
  border: 1px solid oklch(0.76 0.18 162 / 0.25);
  border-radius: 2px;
  color: var(--profit);
  letter-spacing: 0.08em;
}

/* ── FOOTER ── */
.footer {
  border-top: 1px solid var(--bg-border);
  padding: 24px clamp(16px, 5vw, 56px);
  display: flex; align-items: center; gap: 20px;
  flex-wrap: wrap;
}
.footer-mono {
  font-family: var(--font-mono);
  font-size: 0.65rem; font-weight: 500;
  color: var(--ink-ghost);
  letter-spacing: 0.08em;
}
.footer-sep { color: var(--bg-border); }
.footer-spacer { flex: 1; }
.footer-link {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  color: var(--ink-ghost);
  text-decoration: none;
  letter-spacing: 0.06em;
  transition: color 0.16s;
}
.footer-link:hover { color: var(--accent); }

/* ── PAGE-LOAD REVEAL SEQUENCE ── */
/* Content is always visible by default — JS enhances only */
.reveal {
  /* intentionally empty — JS adds .will-animate before first paint */
}
.will-animate .reveal {
  opacity: 0;
  transform: translateY(18px);
}
.will-animate .reveal.in {
  opacity: 1;
  transform: translateY(0);
  transition: opacity 0.7s var(--ease-expo), transform 0.7s var(--ease-expo);
}

@media (prefers-reduced-motion: reduce) {
  .will-animate .reveal,
  .will-animate .reveal.in {
    opacity: 1 !important;
    transform: none !important;
    transition: none !important;
  }
  @keyframes pulse-dot { 0%, 100% { opacity: 1; } }
  .btn-primary { transition: background 0.1s; }
  .btn-primary:hover { transform: none; }
  #seed-canvas { animation: none !important; }
}

/* ── RESPONSIVE ── */
@media (max-width: 768px) {
  .stats-row { grid-template-columns: 1fr 1fr; }
  .stat-cell:nth-child(2) { border-right: none; }
  .stat-cell:nth-child(3) { border-right: 1px solid var(--bg-border); }
  .specs-section { grid-template-columns: 1fr; }
  .spec-block { padding: 24px 20px; }
  .nav-chip { display: none; }
  .hero-heading { letter-spacing: -0.035em; }
}
@media (max-width: 480px) {
  .stats-row { grid-template-columns: 1fr; }
  .stat-cell { border-right: none; border-bottom: 1px solid var(--bg-border); }
  .stat-cell:last-child { border-bottom: none; }
  .hv-meta { gap: 12px; }
  .live-chip { font-size: 0.62rem; }
}

/* ── HAMBURGER NAV ── */
.hb-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.hb-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  background: none;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--ink-muted);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  flex-shrink: 0;
}
.hb-btn:hover,
.hb-btn:focus-visible { color: var(--ink-primary); border-color: var(--bg-border); outline: none; }
.hb-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.hb-dropdown {
  position: fixed;
  top: 60px;
  right: 16px;
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 8px;
  padding: 4px;
  min-width: 160px;
  z-index: 9999;
  box-shadow: 0 8px 24px rgba(0,0,0,0.45);
  opacity: 0;
  transform: translateY(-6px);
  pointer-events: none;
  transition: opacity 0.18s ease-out, transform 0.18s ease-out;
}
@media (prefers-reduced-motion: reduce) {
  .hb-dropdown { transition: none; }
}
.hb-dropdown.hb-open {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}
.hb-item {
  display: block;
  padding: 10px 14px;
  font-family: var(--font-mono);
  font-size: 0.72rem;
  letter-spacing: 0.04em;
  color: var(--ink-secondary);
  text-decoration: none;
  border-radius: 5px;
  transition: background 0.12s, color 0.12s;
}
.hb-item:hover,
.hb-item:focus-visible { background: var(--bg-border); color: var(--ink-primary); outline: none; }
.hb-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.hb-active { color: var(--accent) !important; }
</style>
</head>
<body>

<!-- ── NAV ── -->
<nav class="nav">
  <a href="/" class="nav-brand">DCA<span>//</span>ENGINE</a>
  <div class="nav-spacer"></div>
  <div class="nav-chip">
    <span class="nav-chip-dot"></span>
    <span id="nav-status-text">LIVE</span>
  </div>
  <div class="hb-wrap">
    <button class="hb-btn" id="hb-btn" aria-label="Menu" aria-expanded="false" aria-haspopup="true">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <rect x="2" y="5"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="9"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="13" width="16" height="2" rx="1" fill="currentColor"/>
      </svg>
    </button>
    <div class="hb-dropdown" id="hb-dropdown" role="menu">
      <a href="/"          class="hb-item hb-active" role="menuitem">Home</a>
      <a href="/dashboard" class="hb-item"            role="menuitem">Dashboard</a>
      <a href="/manual"    class="hb-item"            role="menuitem">Manual</a>
    </div>
  </div>
</nav>

<!-- ── HERO ── -->
<section class="hero">
  <canvas id="seed-canvas"></canvas>

  <div class="hero-inner">
    <div class="session-tag reveal">
      OUTPUT&nbsp;·&nbsp;SEED&nbsp;<span class="session-tag-id" id="session-seed">—</span>
      &nbsp;&nbsp;/&nbsp;&nbsp;
      SESSION&nbsp;<span class="session-tag-id" id="session-time">—</span>
    </div>

    <h1 class="hero-heading reveal">
      Precision<br>
      <span class="hero-heading-dim">DCA Scalp</span>
    </h1>

    <p class="hero-body reveal">
      1h timeframe. Red-candle entry. Weighted-average take-profit.
      Running live on Binance USD-M Futures across top-ranked altcoin pairs,
      validated on 9,311 bars per symbol.
    </p>

    <div class="hero-actions reveal">
      <a href="/dashboard" class="btn-primary">→ Open Panel</a>
      <a href="#specs" class="btn-secondary">
        View specs <span class="btn-secondary-arrow">↓</span>
      </a>
    </div>

    <div class="live-strip reveal" id="live-strip">
      <!-- populated by JS -->
    </div>
  </div>

  <!-- Live equity mini-chart -->
  <div class="hero-visual reveal" id="hero-visual-wrap">
    <div class="hv-header">
      <span class="hv-label">EQUITY CURVE — ALL TRADES</span>
      <div class="hv-meta">
        <div class="hv-stat">
          <div class="hv-stat-label">NET PNL</div>
          <div class="hv-stat-value" id="lp-net-pnl">—</div>
        </div>
        <div class="hv-stat">
          <div class="hv-stat-label">TODAY</div>
          <div class="hv-stat-value" id="lp-today-pnl">—</div>
        </div>
        <div class="hv-stat">
          <div class="hv-stat-label">TRADES</div>
          <div class="hv-stat-value" id="lp-total-trades">—</div>
        </div>
      </div>
    </div>
    <div class="canvas-wrap">
      <canvas id="lp-equity-canvas"></canvas>
      <div class="canvas-empty" id="lp-chart-empty">WAITING FOR FIRST SIGNAL</div>
    </div>
  </div>
</section>

<!-- ── STATS ROW ── -->
<section class="stats-section">
  <div class="stats-row">
    <div class="stat-cell reveal">
      <div class="stat-cell-label">WIN RATE</div>
      <div class="stat-cell-value profit" id="lp-win-rate">—</div>
      <div class="stat-cell-sub" id="lp-wr-sub">Breakeven —</div>
    </div>
    <div class="stat-cell reveal">
      <div class="stat-cell-label">AVG WIN</div>
      <div class="stat-cell-value profit" id="lp-avg-win">—</div>
      <div class="stat-cell-sub">per closed trade</div>
    </div>
    <div class="stat-cell reveal">
      <div class="stat-cell-label">BALANCE</div>
      <div class="stat-cell-value" id="lp-balance">—</div>
      <div class="stat-cell-sub">USDT futures</div>
    </div>
    <div class="stat-cell reveal">
      <div class="stat-cell-label">ACTIVE BASKETS</div>
      <div class="stat-cell-value" id="lp-baskets">—</div>
      <div class="stat-cell-sub">open positions</div>
    </div>
  </div>
</section>

<!-- ── SPECS ── -->
<section id="specs">
  <div class="specs-section">
    <div class="spec-block reveal">
      <div class="spec-block-tag">// EXECUTION</div>
      <table class="spec-table">
        <tr><td>Timeframe</td><td class="val-accent">1h</td></tr>
        <tr><td>Entry trigger</td><td>Red candle close</td></tr>
        <tr><td>Position model</td><td>DCA (linear spacing)</td></tr>
        <tr><td>Price drop step</td><td>0.50%</td></tr>
        <tr><td>Size multiplier</td><td>1.1× per safety</td></tr>
        <tr><td>Leverage</td><td>10×</td></tr>
        <tr><td>Margin per trade</td><td>$10</td></tr>
      </table>
    </div>
    <div class="spec-block reveal">
      <div class="spec-block-tag">// VALIDATION</div>
      <table class="spec-table">
        <tr><td>Train window</td><td>Jun 2025 → Feb 2026 (70%)</td></tr>
        <tr><td>OOS window</td><td>Feb → Jun 2026 (30%)</td></tr>
        <tr><td>Bars per symbol</td><td>9,311 1h candles</td></tr>
        <tr><td>Grid search</td><td>TP% × DCA orders</td></tr>
        <tr><td>Top OOS exp.</td><td class="val-profit">+$0.149 / trade (INJ)</td></tr>
        <tr><td>Top win rate</td><td class="val-profit">91.1% (TIA)</td></tr>
        <tr><td>Stop-loss</td><td>Optional — DCA caps downside</td></tr>
      </table>
    </div>
  </div>
</section>

<!-- ── SYMBOL TABLE ── -->
<section class="symbols-section">
  <div class="section-heading">LIVE SYMBOLS — RANKED BY OOS EXPECTANCY</div>
  <div class="sym-table-wrap">
    <table class="sym-table">
      <thead>
        <tr>
          <th>Symbol</th>
          <th>TP %</th>
          <th>DCA Orders</th>
          <th>Win Rate</th>
          <th>OOS Exp ($/trade)</th>
          <th>Net ($)</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>INJUSDT</td><td>0.8</td><td>3</td><td class="col-wr">79.5%</td><td class="col-net">+0.149</td><td class="col-net">+101.07</td><td><span class="sym-live-badge">● LIVE</span></td></tr>
        <tr><td>NEARUSDT</td><td>1.0</td><td>5*</td><td class="col-wr">80.3%</td><td class="col-net">+0.148</td><td class="col-net">+101.06</td><td><span class="sym-live-badge">● LIVE</span></td></tr>
        <tr><td>TIAUSDT</td><td>0.5</td><td>3</td><td class="col-wr">91.1%</td><td class="col-net">+0.119</td><td class="col-net">+82.06</td><td><span class="sym-live-badge">● LIVE</span></td></tr>
        <tr><td>OPUSDT</td><td>0.5</td><td>3</td><td class="col-wr">90.7%</td><td class="col-net">+0.111</td><td class="col-net">+77.87</td><td><span class="sym-live-badge">● LIVE</span></td></tr>
        <tr><td>APTUSDT</td><td>0.5</td><td>3</td><td class="col-wr">87.3%</td><td class="col-net">+0.110</td><td class="col-net">+75.53</td><td><span class="sym-live-badge">● LIVE</span></td></tr>
        <tr><td>DOTUSDT</td><td>0.5</td><td>3</td><td class="col-wr">85.9%</td><td class="col-net">+0.100</td><td class="col-net">+68.11</td><td></td></tr>
        <tr><td>SOLUSDT</td><td>0.5</td><td>3</td><td class="col-wr">72.8%</td><td class="col-net">+0.056</td><td class="col-net">+38.42</td><td></td></tr>
        <tr><td>ETHUSDT</td><td>0.5</td><td>3</td><td class="col-wr">68.3%</td><td class="col-net">+0.048</td><td class="col-net">+33.78</td><td></td></tr>
        <tr><td>BTCUSDT</td><td>0.3</td><td>5</td><td class="col-wr">67.4%</td><td class="col-net">+0.012</td><td class="col-net">+7.94</td><td></td></tr>
        <tr><td>TRXUSDT</td><td>—</td><td>—</td><td class="col-neg">35.3%</td><td class="col-neg">−0.009</td><td class="col-neg">—</td><td></td></tr>
      </tbody>
    </table>
  </div>
</section>

<!-- ── FOOTER ── -->
<footer class="footer">
  <span class="footer-mono">DCA ENGINE</span>
  <span class="footer-sep">·</span>
  <span class="footer-mono">1h · BINANCE USD-M FUTURES · 10×</span>
  <span class="footer-spacer"></span>
  <a href="/dashboard" class="footer-link">→ INSTRUMENTATION PANEL</a>
</footer>

<script>
// ──────────────────────────────────────────────────────────
// SEED CANVAS MOTIF — generative dot-grid background
// ──────────────────────────────────────────────────────────
(function() {
  const canvas = document.getElementById('seed-canvas');
  const ctx = canvas.getContext('2d');
  let W, H, dots = [], animId;
  const SEED = Math.floor(Math.random() * 4294967296);
  document.getElementById('session-seed').textContent = SEED;
  document.getElementById('session-time').textContent = new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC';

  function lcg(s) { return (((s * 1664525 + 1013904223) >>> 0) / 4294967296); }

  function buildDots() {
    dots = [];
    const cols = Math.ceil(W / 36);
    const rows = Math.ceil(H / 36);
    let s = SEED;
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        s = (s * 1664525 + 1013904223) >>> 0;
        const rand = s / 4294967296;
        if (rand < 0.32) {
          dots.push({
            x: c * 36 + 18,
            y: r * 36 + 18,
            r: rand * 1.8 + 0.4,
            phase: rand * Math.PI * 2,
            speed: 0.5 + rand * 0.8,
          });
        }
      }
    }
  }

  function resize() {
    W = canvas.offsetWidth;
    H = canvas.offsetHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.scale(dpr, dpr);
    buildDots();
  }

  let t = 0;
  function draw() {
    ctx.clearRect(0, 0, W, H);
    const now = t * 0.004;
    for (const d of dots) {
      const alpha = 0.1 + 0.18 * Math.sin(now * d.speed + d.phase);
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fillStyle = `oklch(0.72 0.17 212 / ${alpha.toFixed(3)})`;
      ctx.fill();
    }
    t++;
    animId = requestAnimationFrame(draw);
  }

  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (prefersReduced) {
    resize();
    // draw once static
    ctx.clearRect(0, 0, W, H);
    for (const d of dots) {
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fillStyle = 'oklch(0.72 0.17 212 / 0.12)';
      ctx.fill();
    }
  } else {
    resize();
    draw();
  }

  window.addEventListener('resize', () => {
    if (animId) cancelAnimationFrame(animId);
    resize();
    if (!prefersReduced) draw();
  });
})();

// ──────────────────────────────────────────────────────────
// PAGE-LOAD REVEAL SEQUENCE
// ──────────────────────────────────────────────────────────
(function() {
  const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (prefersReduced) return;

  document.body.classList.add('will-animate');
  const els = document.querySelectorAll('.reveal');

  // Stagger reveal: each element delays 80ms after the previous
  els.forEach((el, i) => {
    setTimeout(() => {
      el.classList.add('in');
    }, 60 + i * 80);
  });
})();

// ──────────────────────────────────────────────────────────
// EQUITY SPARKLINE (hero)
// ──────────────────────────────────────────────────────────
function drawLandingChart(canvas, trades) {
  const wrap = canvas.parentElement;
  const empty = document.getElementById('lp-chart-empty');
  const W = wrap.clientWidth;
  const H = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  if (!trades || trades.length < 1) {
    empty.style.display = 'flex';
    return;
  }
  empty.style.display = 'none';

  const pL = 0, pR = 0, pT = 8, pB = 8;
  const plotW = W - pL - pR;
  const plotH = H - pT - pB;

  let running = 0;
  const pts = [{y: 0}];
  trades.forEach(t => { running += parseFloat(t.net_pnl || 0); pts.push({y: running}); });

  const vals = pts.map(p => p.y);
  const minV = Math.min(...vals, 0);
  const maxV = Math.max(...vals, 0.001);
  const range = maxV - minV || 0.001;
  const xS = i => pL + (i / (pts.length - 1 || 1)) * plotW;
  const yS = v => pT + plotH - ((v - minV) / range) * plotH;

  // Grid
  ctx.strokeStyle = 'oklch(0.25 0.014 248 / 0.8)';
  ctx.lineWidth = 0.5;
  for (let g = 0; g <= 3; g++) {
    const y = pT + (g / 3) * plotH;
    ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(W - pR, y); ctx.stroke();
  }

  // Zero line
  if (minV < 0 && maxV > 0) {
    const zy = yS(0);
    ctx.strokeStyle = 'oklch(0.52 0.015 248 / 0.4)';
    ctx.lineWidth = 1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pL, zy); ctx.lineTo(W - pR, zy); ctx.stroke();
    ctx.setLineDash([]);
  }

  const isPos = running >= 0;
  const cProfit = 'oklch(0.76 0.18 162)';
  const cLoss   = 'oklch(0.68 0.22 22)';
  const cMain   = isPos ? cProfit : cLoss;

  // Fill
  const grad = ctx.createLinearGradient(0, pT, 0, pT + plotH);
  if (isPos) {
    grad.addColorStop(0, 'oklch(0.76 0.18 162 / 0.22)');
    grad.addColorStop(1, 'oklch(0.76 0.18 162 / 0.01)');
  } else {
    grad.addColorStop(0, 'oklch(0.68 0.22 22 / 0.22)');
    grad.addColorStop(1, 'oklch(0.68 0.22 22 / 0.01)');
  }
  ctx.beginPath();
  pts.forEach((p,i) => { i===0 ? ctx.moveTo(xS(i), yS(p.y)) : ctx.lineTo(xS(i), yS(p.y)); });
  ctx.lineTo(xS(pts.length-1), pT+plotH);
  ctx.lineTo(xS(0), pT+plotH);
  ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  // Glow line
  ctx.beginPath();
  pts.forEach((p,i) => { i===0 ? ctx.moveTo(xS(i), yS(p.y)) : ctx.lineTo(xS(i), yS(p.y)); });
  ctx.strokeStyle = isPos ? 'oklch(0.76 0.18 162 / 0.28)' : 'oklch(0.68 0.22 22 / 0.28)';
  ctx.lineWidth = 7; ctx.stroke();

  // Line
  ctx.beginPath();
  pts.forEach((p,i) => { i===0 ? ctx.moveTo(xS(i), yS(p.y)) : ctx.lineTo(xS(i), yS(p.y)); });
  ctx.strokeStyle = cMain; ctx.lineWidth = 2; ctx.stroke();

  // Endpoint
  const lx = xS(pts.length-1), ly = yS(pts[pts.length-1].y);
  ctx.beginPath(); ctx.arc(lx, ly, 3.5, 0, Math.PI*2);
  ctx.fillStyle = cMain; ctx.fill();
  ctx.beginPath(); ctx.arc(lx, ly, 6, 0, Math.PI*2);
  ctx.strokeStyle = isPos ? 'oklch(0.76 0.18 162 / 0.35)' : 'oklch(0.68 0.22 22 / 0.35)';
  ctx.lineWidth = 2; ctx.stroke();
}

// ──────────────────────────────────────────────────────────
// DATA FETCH & DOM UPDATE
// ──────────────────────────────────────────────────────────
let _lastData = null;

async function refreshLanding() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    _lastData = d;

    // Nav status
    document.getElementById('nav-status-text').textContent = d.bot_alive ? 'LIVE' : 'STOPPED';

    // Hero chart
    const canvas = document.getElementById('lp-equity-canvas');
    drawLandingChart(canvas, d.trades_chrono || []);

    // Hero PnL stats
    const net = d.net_pnl || 0;
    const netEl = document.getElementById('lp-net-pnl');
    netEl.textContent = (net >= 0 ? '+' : '') + net.toFixed(4) + ' USDT';
    netEl.className = 'hv-stat-value' + (net >= 0 ? ' profit' : '');

    const todEl = document.getElementById('lp-today-pnl');
    const tod = d.today_pnl || 0;
    todEl.textContent = (tod >= 0 ? '+' : '') + tod.toFixed(4);
    todEl.className = 'hv-stat-value' + (tod >= 0 ? ' profit' : '');

    document.getElementById('lp-total-trades').textContent = d.total_trades || '0';

    // Stats row
    const wr = (d.win_rate || 0) * 100;
    document.getElementById('lp-win-rate').textContent = wr > 0 ? wr.toFixed(1) + '%' : '—';
    const be = (d.breakeven_winrate || 0) * 100;
    document.getElementById('lp-wr-sub').textContent = 'Breakeven ' + (be > 0 ? be.toFixed(1) + '%' : '—');
    document.getElementById('lp-avg-win').textContent = d.avg_win ? '+' + d.avg_win.toFixed(4) + ' USDT' : '—';
    document.getElementById('lp-balance').textContent = d.balance ? d.balance.toFixed(2) + ' USDT' : '—';
    document.getElementById('lp-baskets').textContent = (d.baskets || []).length;

    // Live symbol chips
    const strip = document.getElementById('live-strip');
    const syms = d.symbols || [];
    if (syms.length && !strip.dataset.built) {
      strip.dataset.built = '1';
      strip.innerHTML = syms.map(s =>
        `<span class="live-chip"><span class="live-chip-name">${s.name || s}</span>` +
        (s.tp_pct ? `&nbsp;<span class="live-chip-tp">TP ${s.tp_pct}%</span>` : '') +
        '</span>'
      ).join('');
    }
  } catch(e) {
    console.warn('landing refresh error', e);
  }
}

refreshLanding();
setInterval(refreshLanding, 8000);

window.addEventListener('resize', () => {
  if (_lastData) drawLandingChart(
    document.getElementById('lp-equity-canvas'),
    _lastData.trades_chrono || []
  );
});

// ── HAMBURGER NAV ──
(function() {
  const btn = document.getElementById('hb-btn');
  const dd  = document.getElementById('hb-dropdown');
  if (!btn || !dd) return;
  function open()  { dd.classList.add('hb-open');    btn.setAttribute('aria-expanded','true'); }
  function close(refocus) { dd.classList.remove('hb-open'); btn.setAttribute('aria-expanded','false'); if (refocus) btn.focus(); }
  function toggle() { dd.classList.contains('hb-open') ? close(true) : open(); }
  btn.addEventListener('click', function(e) { e.stopPropagation(); toggle(); });
  document.addEventListener('click', function(e) {
    if (dd.classList.contains('hb-open') && !dd.contains(e.target) && e.target !== btn) close(false);
  });
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && dd.classList.contains('hb-open')) close(true); });
})();
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD PAGE (Instrumentation Panel — redesigned)
# ──────────────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCA Engine — Instrumentation Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ── Z-INDEX SCALE ── */
:root {
  --z-base:     0;
  --z-overlay:  10;
  --z-nav:      20;
  --z-dropdown: 200;
}

/* ── OKLCH COLOR TOKENS ── */
:root {
  --bg-void:    oklch(0.14 0.012 248);
  --bg-surface: oklch(0.18 0.014 248);
  --bg-raised:  oklch(0.21 0.014 248);
  --bg-border:  oklch(0.25 0.014 248);
  --ink-primary:   oklch(0.94 0.008 248);
  --ink-secondary: oklch(0.72 0.015 248);
  --ink-muted:     oklch(0.52 0.015 248);
  --ink-ghost:     oklch(0.38 0.012 248);
  --profit:     oklch(0.76 0.18 162);
  --profit-dim: oklch(0.76 0.18 162 / 0.12);
  --loss:       oklch(0.68 0.22 22);
  --loss-dim:   oklch(0.68 0.22 22 / 0.12);
  --accent:     oklch(0.72 0.17 212);
  --accent-dim: oklch(0.72 0.17 212 / 0.12);
  --amber:      oklch(0.78 0.17 75);
  --chart-grid: oklch(0.25 0.014 248);
  --font-body: 'Manrope', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Mono', monospace;
  --ease-expo: cubic-bezier(0.16, 1, 0.3, 1);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 15px; }

body {
  font-family: var(--font-body);
  background: var(--bg-void);
  color: var(--ink-primary);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  overflow-x: hidden;
  line-height: 1.6;
}

/* ── TOP BAR ── */
.topbar {
  position: sticky; top: 0; z-index: var(--z-nav);
  display: flex; align-items: center; gap: 0;
  height: 48px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--bg-border);
}
.topbar-brand {
  display: flex; align-items: center;
  padding: 0 20px;
  height: 100%;
  border-right: 1px solid var(--bg-border);
  font-family: var(--font-mono);
  font-size: 0.72rem; font-weight: 600;
  color: var(--ink-primary);
  text-decoration: none;
  letter-spacing: 0.04em;
  flex-shrink: 0;
}
.topbar-brand span { color: var(--accent); }
.topbar-items {
  display: flex; align-items: center; gap: 0;
  flex: 1; height: 100%;
  overflow-x: auto;
}
.topbar-item {
  display: flex; align-items: center; gap: 8px;
  padding: 0 18px;
  height: 100%;
  border-right: 1px solid var(--bg-border);
  font-family: var(--font-mono);
  font-size: 0.68rem;
  white-space: nowrap;
}
.topbar-item-label { color: var(--ink-ghost); letter-spacing: 0.06em; }
.topbar-item-value { color: var(--ink-primary); font-weight: 500; }
.topbar-dot {
  width: 6px; height: 6px; border-radius: 50%;
  flex-shrink: 0;
}
.dot-live { background: var(--profit); box-shadow: 0 0 6px var(--profit); animation: pulse-dot 2.4s ease-in-out infinite; }
.dot-off  { background: var(--ink-ghost); }
.dot-amber { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
@keyframes pulse-dot {
  0%,100% { opacity:1; box-shadow: 0 0 6px var(--profit); }
  50%      { opacity:0.6; box-shadow: 0 0 10px var(--profit); }
}
.topbar-spacer { flex: 1; }
.topbar-streak {
  display: flex; align-items: center; gap: 8px;
  padding: 0 18px; height: 100%;
  border-left: 1px solid var(--bg-border);
  font-family: var(--font-mono); font-size: 0.68rem;
  flex-shrink: 0;
}
.streak-num { font-weight: 600; font-size: 0.85rem; color: var(--profit); }
.streak-num.loss { color: var(--loss); }
.streak-label { color: var(--ink-ghost); }
.topbar-back {
  display: flex; align-items: center; gap: 6px;
  padding: 0 16px; height: 100%;
  border-left: 1px solid var(--bg-border);
  font-family: var(--font-mono); font-size: 0.66rem;
  color: var(--ink-muted); text-decoration: none;
  transition: color 0.16s;
  flex-shrink: 0;
}
.topbar-back:hover { color: var(--accent); }

/* ── BOT START/STOP CONTROL (topbar) ── */
.topbar-botctl {
  display: flex; align-items: center; gap: 6px;
  padding: 0 12px; margin: 0 4px; height: 100%;
  border-left: 1px solid var(--bg-border);
  border-right: 1px solid var(--bg-border);
  flex-shrink: 0;
}
.botctl-status {
  font-family: var(--font-mono); font-size: 0.58rem;
  letter-spacing: 0.06em; color: var(--ink-muted);
  white-space: nowrap;
}
.botctl-btn {
  display: inline-flex; align-items: center; gap: 4px;
  min-width: 36px; min-height: 28px; padding: 0 8px;
  font-family: var(--font-mono); font-size: 0.56rem; font-weight: 600;
  letter-spacing: 0.06em; border-radius: 4px; border: 1px solid;
  cursor: pointer; transition: opacity 0.15s, background 0.15s;
  white-space: nowrap;
}
.botctl-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.botctl-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.botctl-start {
  background: rgba(0,200,80,0.12);
  border-color: var(--profit);
  color: var(--profit);
}
.botctl-start:not(:disabled):hover { background: rgba(0,200,80,0.22); }
.botctl-stop {
  background: rgba(230,60,60,0.12);
  border-color: var(--loss);
  color: var(--loss);
}
.botctl-stop:not(:disabled):hover { background: rgba(230,60,60,0.22); }
.botctl-msg {
  font-family: var(--font-mono); font-size: 0.6rem;
  color: var(--loss); max-width: 120px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
.botctl-spinner {
  width: 16px; height: 16px; color: var(--ink-muted); flex-shrink: 0;
}
@media (prefers-reduced-motion: reduce) {
  .botctl-spinner circle { animation: none; stroke-dasharray: 56 0; }
}

/* ── HAMBURGER NAV (dashboard) ── */
.hb-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.hb-btn {
  display: flex; align-items: center; justify-content: center;
  width: 44px; height: 44px;
  background: none; border: 1px solid transparent; border-radius: 6px;
  color: var(--ink-muted); cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  flex-shrink: 0;
}
.hb-btn:hover { color: var(--ink-primary); border-color: var(--bg-border); }
.hb-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; color: var(--ink-primary); border-color: var(--bg-border); }
.hb-dropdown {
  position: fixed; top: 56px; right: 8px;
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 8px; padding: 4px; min-width: 160px;
  z-index: var(--z-dropdown);
  box-shadow: 0 8px 24px rgba(0,0,0,0.55);
  opacity: 0; transform: translateY(-6px);
  pointer-events: none;
  transition: opacity 0.18s ease-out, transform 0.18s ease-out;
}
@media (prefers-reduced-motion: reduce) {
  .hb-dropdown { transition: none; }
}
.hb-dropdown.hb-open { opacity: 1; transform: translateY(0); pointer-events: auto; }
.hb-item {
  display: block; padding: 10px 14px;
  font-family: var(--font-mono); font-size: 0.72rem;
  letter-spacing: 0.04em; color: var(--ink-secondary);
  text-decoration: none; border-radius: 5px;
  transition: background 0.12s, color 0.12s;
}
.hb-item:hover { background: var(--bg-border); color: var(--ink-primary); }
.hb-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; background: var(--bg-border); color: var(--ink-primary); }
.hb-active { color: var(--accent) !important; }

/* ── EQUITY STRIP ── */
.equity-section {
  background: var(--bg-surface);
  border-bottom: 1px solid var(--bg-border);
  padding: 16px 20px 14px;
}
.equity-header {
  display: flex; justify-content: space-between; align-items: flex-end;
  margin-bottom: 10px;
}
.equity-tag {
  font-family: var(--font-mono);
  font-size: 0.6rem; font-weight: 500;
  color: var(--ink-ghost);
  letter-spacing: 0.1em; text-transform: uppercase;
}
.equity-stats {
  display: flex; gap: 24px;
}
.equity-stat { text-align: right; }
.equity-stat-label {
  font-family: var(--font-mono);
  font-size: 0.58rem; color: var(--ink-ghost);
  letter-spacing: 0.08em; text-transform: uppercase;
}
.equity-stat-value {
  font-family: var(--font-mono);
  font-size: 1rem; font-weight: 600;
}
.equity-canvas-wrap {
  position: relative; width: 100%; height: 130px;
  background: var(--bg-raised);
  border: 1px solid var(--bg-border);
  border-radius: 4px; overflow: hidden;
}
.equity-canvas-wrap canvas { width: 100%; height: 100%; display: block; }
.equity-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono); font-size: 0.72rem;
  color: var(--ink-ghost); letter-spacing: 0.06em;
}

/* ── METRICS STRIP ── */
.metrics {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 0;
  background: var(--bg-border);
  border-bottom: 1px solid var(--bg-border);
}
.metric {
  background: var(--bg-surface);
  padding: 13px 16px;
  border-right: 1px solid var(--bg-border);
  display: flex; flex-direction: column;
}
.metric:last-child { border-right: none; }
.metric-label {
  font-family: var(--font-mono);
  font-size: 0.58rem; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-ghost); margin-bottom: 5px;
}
.metric-value {
  font-family: var(--font-mono);
  font-size: 1.2rem; font-weight: 600; line-height: 1.15;
}
.metric-sub {
  font-family: var(--font-mono);
  font-size: 0.62rem; color: var(--ink-muted); margin-top: 2px;
}

/* Semantic colors */
.text-profit { color: var(--profit); }
.text-loss   { color: var(--loss); }
.text-amber  { color: var(--amber); }
.text-accent { color: var(--accent); }
.text-primary { color: var(--ink-primary); }
.text-muted  { color: var(--ink-muted); }

/* ── TABLES ── */
.tables {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 0; background: var(--bg-border); flex: 1;
}
.table-panel {
  background: var(--bg-surface);
  display: flex; flex-direction: column;
  border-right: 1px solid var(--bg-border);
}
.table-panel:last-child { border-right: none; }
.table-panel-header {
  display: flex; align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--bg-border);
}
.table-panel-tag {
  font-family: var(--font-mono);
  font-size: 0.58rem; font-weight: 500;
  color: var(--ink-ghost);
  letter-spacing: 0.1em; text-transform: uppercase;
}
.table-wrap { overflow-x: auto; flex: 1; }
table {
  width: 100%; border-collapse: collapse;
  font-family: var(--font-mono); font-size: 0.69rem;
}
th {
  text-align: left; padding: 8px 10px;
  font-family: var(--font-mono); font-size: 0.58rem; font-weight: 500;
  color: var(--ink-ghost); letter-spacing: 0.08em; text-transform: uppercase;
  border-bottom: 1px solid var(--bg-border);
}
td {
  padding: 7px 10px;
  border-bottom: 1px solid oklch(0.25 0.014 248 / 0.6);
  color: var(--ink-secondary);
  transition: background 0.12s;
}
td:first-child { color: var(--ink-primary); }
tr:hover td { background: var(--bg-raised); }
.badge {
  display: inline-flex; align-items: center;
  padding: 2px 7px; border-radius: 2px;
  font-size: 0.6rem; font-weight: 600;
  letter-spacing: 0.06em;
}
.badge-win {
  background: var(--profit-dim);
  border: 1px solid oklch(0.76 0.18 162 / 0.2);
  color: var(--profit);
}
.badge-loss {
  background: var(--loss-dim);
  border: 1px solid oklch(0.68 0.22 22 / 0.2);
  color: var(--loss);
}
.badge-long {
  font-size:0.65rem;
  padding:1px 5px;
  border-radius:4px;
  background:oklch(0.35 0.08 160 / 0.25);
  border:1px solid oklch(0.55 0.15 160 / 0.3);
}
.badge-short {
  font-size:0.65rem;
  padding:1px 5px;
  border-radius:4px;
  background:oklch(0.35 0.08 20 / 0.25);
  border:1px solid oklch(0.55 0.15 20 / 0.3);
}
.badge-active {
  background: var(--accent-dim);
  border: 1px solid oklch(0.72 0.17 212 / 0.2);
  color: var(--accent);
}
.empty-state {
  padding: 36px 16px; text-align: center;
  font-family: var(--font-mono); font-size: 0.72rem;
  color: var(--ink-ghost); letter-spacing: 0.06em;
}

/* ── REVEAL ANIMATION ── */
.reveal { /* content visible by default */ }
.will-animate .reveal {
  opacity: 0; transform: translateY(12px);
}
.will-animate .reveal.in {
  opacity: 1; transform: none;
  transition: opacity 0.55s var(--ease-expo), transform 0.55s var(--ease-expo);
}
@media (prefers-reduced-motion: reduce) {
  .will-animate .reveal,
  .will-animate .reveal.in {
    opacity: 1 !important; transform: none !important; transition: none !important;
  }
  .topbar-dot { animation: none !important; }
}

/* ── RESPONSIVE ── */
@media (max-width: 900px) {
  .metrics { grid-template-columns: repeat(3, 1fr); }
  .tables  { grid-template-columns: 1fr; }
  .equity-section { padding: 12px 14px; }
  .topbar-item:nth-child(n+4) { display: none; }
}
@media (max-width: 500px) {
  .metrics { grid-template-columns: repeat(2, 1fr); }
  .equity-canvas-wrap { height: 90px; }
  .topbar-item { padding: 0 12px; }
  .topbar-brand { padding: 0 12px; }
}
</style>
</head>
<body>

<!-- ── TOP BAR ── -->
<header class="topbar">
  <a href="/" class="topbar-brand">DCA<span>//</span>ENGINE</a>
  <div class="topbar-items">
    <div class="topbar-item">
      <span class="topbar-item-label">BALANCE</span>
      <span class="topbar-item-value" id="balance">—</span>
      <span class="topbar-item-label">USDT</span>
    </div>
    <div class="topbar-spacer"></div>
  </div>
  <div class="topbar-botctl" id="topbar-botctl">
    <span class="botctl-status" id="botctl-status-text">—</span>
    <button class="botctl-btn botctl-start" id="botctl-start" aria-label="Start auto bot">
      <svg width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden="true"><polygon points="2,1 11,6 2,11" fill="currentColor"/></svg>
      START
    </button>
    <button class="botctl-btn botctl-stop" id="botctl-stop" style="display:none" aria-label="Stop auto bot">
      <svg width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden="true"><rect x="2" y="2" width="8" height="8" rx="1" fill="currentColor"/></svg>
      STOP
    </button>
    <span class="botctl-msg" id="botctl-msg"></span>
    <svg class="botctl-spinner" id="botctl-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="display:none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" stroke-dasharray="28 56" stroke-linecap="round">
        <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/>
      </circle>
    </svg>
  </div>
  <div class="topbar-streak">
    <span class="streak-num" id="streak-num">0</span>
    <span class="streak-label" id="streak-label">streak</span>
  </div>
  <div class="hb-wrap">
    <button class="hb-btn" id="hb-btn" aria-label="Menu" aria-expanded="false" aria-haspopup="true">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <rect x="2" y="5"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="9"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="13" width="16" height="2" rx="1" fill="currentColor"/>
      </svg>
    </button>
    <div class="hb-dropdown" id="hb-dropdown" role="menu">
      <a href="/"          class="hb-item"            role="menuitem">Home</a>
      <a href="/dashboard" class="hb-item hb-active"  role="menuitem">Dashboard</a>
      <a href="/manual"    class="hb-item"            role="menuitem">Manual</a>
    </div>
  </div>
</header>

<!-- ── EQUITY STRIP ── -->
<section class="equity-section reveal">
  <div class="equity-header">
    <span class="equity-tag">EQUITY CURVE</span>
    <div class="equity-stats">
      <div class="equity-stat">
        <div class="equity-stat-label">NET PNL</div>
        <div class="equity-stat-value" id="net-pnl">—</div>
      </div>
      <div class="equity-stat">
        <div class="equity-stat-label">TODAY</div>
        <div class="equity-stat-value" id="today-pnl">—</div>
      </div>
      <div class="equity-stat">
        <div class="equity-stat-label">PEAK</div>
        <div class="equity-stat-value text-profit" id="peak-pnl">—</div>
      </div>
    </div>
  </div>
  <div class="equity-canvas-wrap">
    <canvas id="equity-canvas"></canvas>
    <div class="equity-empty" id="chart-empty">NO TRADES — WAITING FOR FIRST SIGNAL</div>
  </div>
</section>

<!-- ── METRICS STRIP ── -->
<div class="metrics">
  <div class="metric reveal">
    <div class="metric-label">Trades</div>
    <div class="metric-value text-primary" id="total-trades">0</div>
  </div>
  <div class="metric reveal">
    <div class="metric-label">Win Rate</div>
    <div class="metric-value" id="win-rate">—</div>
    <div class="metric-sub" id="win-rate-sub"></div>
  </div>
  <div class="metric reveal">
    <div class="metric-label">Avg Win</div>
    <div class="metric-value text-profit" id="avg-win">—</div>
  </div>
  <div class="metric reveal">
    <div class="metric-label">Avg Loss</div>
    <div class="metric-value text-loss" id="avg-loss">—</div>
  </div>
  <div class="metric reveal">
    <div class="metric-label">Best</div>
    <div class="metric-value text-profit" id="best-trade">—</div>
  </div>
  <div class="metric reveal">
    <div class="metric-label">Worst</div>
    <div class="metric-value text-loss" id="worst-trade">—</div>
  </div>
</div>

<!-- ── TABLES ── -->
<div class="tables" style="flex:1">
  <div class="table-panel reveal">
    <div class="table-panel-header">
      <span class="table-panel-tag">RECENT TRADES</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Symbol</th><th>Entry</th><th>Exit</th>
          <th>PnL</th><th>%</th><th>Reason</th><th>#S</th>
        </tr></thead>
        <tbody id="trades-body">
          <tr><td colspan="8"><div class="empty-state">WAITING FOR FIRST TRADE</div></td></tr>
        </tbody>
      </table>
    </div>
  </div>
  <div class="table-panel reveal">
    <div class="table-panel-header">
      <span class="table-panel-tag">ACTIVE BASKETS</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Dir</th><th>Entry@</th><th>Avg</th><th>Mark</th>
          <th>Δ Entry</th><th>DCA Levels</th><th>Float PnL</th><th>Status</th>
        </tr></thead>
        <tbody id="baskets-body">
          <tr><td colspan="9"><div class="empty-state">NO ACTIVE POSITIONS</div></td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
// ──────────────────────────────────────────────────────────
// REVEAL SEQUENCE
// ──────────────────────────────────────────────────────────
(function() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  document.body.classList.add('will-animate');
  document.querySelectorAll('.reveal').forEach((el, i) => {
    setTimeout(() => el.classList.add('in'), 40 + i * 60);
  });
})();

// ──────────────────────────────────────────────────────────
// EQUITY CHART
// ──────────────────────────────────────────────────────────
function drawChart(canvas, trades) {
  const wrap = canvas.parentElement;
  const empty = document.getElementById('chart-empty');
  const W = wrap.clientWidth;
  const H = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width  = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  if (!trades || trades.length < 1) { empty.style.display = 'flex'; return; }
  empty.style.display = 'none';

  const pL=0, pR=0, pT=8, pB=8;
  const plotW = W - pL - pR;
  const plotH = H - pT - pB;

  let running = 0;
  const pts = [{y:0}];
  trades.forEach(t => { running += parseFloat(t.net_pnl || 0); pts.push({y: running}); });

  const vals = pts.map(p => p.y);
  const minV = Math.min(...vals, 0);
  const maxV = Math.max(...vals, 0.001);
  const range = maxV - minV || 0.001;
  const xS = i => pL + (i / (pts.length - 1 || 1)) * plotW;
  const yS = v => pT + plotH - ((v - minV) / range) * plotH;

  // Grid
  ctx.strokeStyle = 'oklch(0.25 0.014 248 / 0.9)';
  ctx.lineWidth = 0.5;
  for (let g = 0; g <= 4; g++) {
    const y = pT + (g/4)*plotH;
    ctx.beginPath(); ctx.moveTo(pL,y); ctx.lineTo(W-pR,y); ctx.stroke();
  }

  // Zero dashed line
  if (minV < 0 && maxV > 0) {
    const zy = yS(0);
    ctx.strokeStyle = 'oklch(0.52 0.015 248 / 0.4)';
    ctx.lineWidth = 1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pL, zy); ctx.lineTo(W-pR, zy); ctx.stroke();
    ctx.setLineDash([]);
  }

  const isPos = running >= 0;

  // Fill
  const grad = ctx.createLinearGradient(0, pT, 0, pT+plotH);
  if (isPos) { grad.addColorStop(0,'oklch(0.76 0.18 162 / 0.22)'); grad.addColorStop(1,'oklch(0.76 0.18 162 / 0.01)'); }
  else        { grad.addColorStop(0,'oklch(0.68 0.22 22 / 0.22)'); grad.addColorStop(1,'oklch(0.68 0.22 22 / 0.01)'); }
  ctx.beginPath();
  pts.forEach((p,i)=>{ i===0?ctx.moveTo(xS(i),yS(p.y)):ctx.lineTo(xS(i),yS(p.y)); });
  ctx.lineTo(xS(pts.length-1), pT+plotH); ctx.lineTo(xS(0), pT+plotH);
  ctx.closePath(); ctx.fillStyle=grad; ctx.fill();

  // Glow
  ctx.beginPath();
  pts.forEach((p,i)=>{ i===0?ctx.moveTo(xS(i),yS(p.y)):ctx.lineTo(xS(i),yS(p.y)); });
  ctx.strokeStyle = isPos?'oklch(0.76 0.18 162 / 0.28)':'oklch(0.68 0.22 22 / 0.28)';
  ctx.lineWidth=7; ctx.stroke();

  // Line
  ctx.beginPath();
  pts.forEach((p,i)=>{ i===0?ctx.moveTo(xS(i),yS(p.y)):ctx.lineTo(xS(i),yS(p.y)); });
  ctx.strokeStyle = isPos?'oklch(0.76 0.18 162)':'oklch(0.68 0.22 22)';
  ctx.lineWidth=2; ctx.stroke();

  // Endpoint
  const lx=xS(pts.length-1), ly=yS(pts[pts.length-1].y);
  ctx.beginPath(); ctx.arc(lx,ly,3,0,Math.PI*2);
  ctx.fillStyle=isPos?'oklch(0.76 0.18 162)':'oklch(0.68 0.22 22)'; ctx.fill();
  ctx.beginPath(); ctx.arc(lx,ly,5.5,0,Math.PI*2);
  ctx.strokeStyle=isPos?'oklch(0.76 0.18 162 / 0.35)':'oklch(0.68 0.22 22 / 0.35)';
  ctx.lineWidth=2; ctx.stroke();
}

// ──────────────────────────────────────────────────────────
// DATA & DOM
// ──────────────────────────────────────────────────────────
let lastData = null;

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    lastData = d;

    // Topbar balance
    document.getElementById('balance').textContent = (d.balance || 0).toFixed(2);

    // Streak
    const consec = d.consecutive_wins || 0;
    const streakNum = document.getElementById('streak-num');
    streakNum.textContent = consec;
    streakNum.className = 'streak-num' + (d.last_trade_result === 'loss' ? ' loss' : '');
    document.getElementById('streak-label').textContent = consec > 0
      ? (d.last_trade_result === 'loss' ? 'losing' : 'winning')
      : 'streak';

    // Equity header
    const net = d.net_pnl || 0;
    const netEl = document.getElementById('net-pnl');
    netEl.textContent = (net >= 0 ? '+' : '') + net.toFixed(4);
    netEl.className = 'equity-stat-value ' + (net >= 0 ? 'text-profit' : 'text-loss');

    const tod = d.today_pnl || 0;
    const todEl = document.getElementById('today-pnl');
    todEl.textContent = (tod >= 0 ? '+' : '') + tod.toFixed(4);
    todEl.className = 'equity-stat-value ' + (tod >= 0 ? 'text-profit' : 'text-loss');

    const peakEl = document.getElementById('peak-pnl');
    peakEl.textContent = '+' + (d.peak_pnl || 0).toFixed(4);

    // Chart
    drawChart(document.getElementById('equity-canvas'), d.trades_chrono || []);

    // Metrics
    document.getElementById('total-trades').textContent = d.total_trades || 0;

    const wr = (d.win_rate || 0) * 100;
    const wrEl = document.getElementById('win-rate');
    wrEl.textContent = wr.toFixed(1) + '%';
    wrEl.className = 'metric-value ' + (wr >= 50 ? 'text-profit' : wr > 0 ? 'text-amber' : 'text-primary');
    const be = (d.breakeven_winrate || 0) * 100;
    document.getElementById('win-rate-sub').textContent = 'BE ' + be.toFixed(1) + '%';

    document.getElementById('avg-win').textContent  = d.avg_win  ? '+' + d.avg_win.toFixed(4)  : '—';
    document.getElementById('avg-loss').textContent = d.avg_loss ? d.avg_loss.toFixed(4)        : '—';
    document.getElementById('best-trade').textContent  = d.best_trade  ? '+' + d.best_trade.toFixed(4)  : '—';
    document.getElementById('worst-trade').textContent = d.worst_trade ? d.worst_trade.toFixed(4) : '—';

    // Trades table
    const tb = document.getElementById('trades-body');
    const recent = d.recent_trades || [];
    if (recent.length) {
      tb.innerHTML = recent.map(t => {
        const pnl = parseFloat(t.net_pnl || 0);
        const isW = pnl > 0;
        const pct = parseFloat(t.pnl_pct || 0);
        const ts = (t.exit_time || '').length >= 19 ? (t.exit_time || '').substr(11,8) : '';
        return `<tr>
          <td>${ts}</td>
          <td>${t.symbol || ''}</td>
          <td>${parseFloat(t.entry_price||0).toFixed(4)}</td>
          <td>${parseFloat(t.exit_price||0).toFixed(4)}</td>
          <td class="${isW?'text-profit':'text-loss'}">${isW?'+':''}${pnl.toFixed(4)}</td>
          <td class="${isW?'text-profit':'text-loss'}">${isW?'+':''}${pct.toFixed(2)}%</td>
          <td><span class="badge ${isW?'badge-win':'badge-loss'}">${t.exit_reason||''}</span></td>
          <td>${t.n_safety_filled||0}</td>
        </tr>`;
      }).join('');
    } else {
      tb.innerHTML = '<tr><td colspan="8"><div class="empty-state">WAITING FOR FIRST TRADE</div></td></tr>';
    }

    // Baskets table
    const bb = document.getElementById('baskets-body');
    const baskets = d.baskets || [];
    if (baskets.length) {
      bb.innerHTML = baskets.map(b => {
        const flPnl = b.floating_pnl != null ? parseFloat(b.floating_pnl) : null;
        const avg = parseFloat(b.avg_entry || 0);
        const cur = b.current_price ? parseFloat(b.current_price) : null;
        const dist = cur ? ((cur - avg) / avg * 100) : null;
        const dcaStr = b.dca_levels && b.dca_levels.length ? b.dca_levels.map(l=>l.toFixed(4)).join(', ') : '—';
        const liqStr = b.liquidation ? parseFloat(b.liquidation).toFixed(4) : '—';
        const ets = b.entry_time && b.entry_time.length >= 19 ? b.entry_time.substr(11,8) : '—';
        const distCls = dist===null?'':(dist>0?'text-profit':'text-loss');
        const flCls = flPnl===null?'':(flPnl>=0?'text-profit':'text-loss');
        const dir = b.direction || 'long';
        const dirIcon = dir === 'short' ? '🔻' : '🟢';
        const dirBadge = `<span class="badge-${dir}">${dirIcon} ${dir.toUpperCase()}</span>`;
        return `<tr>
          <td>${b.symbol||''}</td>
          <td>${dirBadge}</td>
          <td>${ets}</td>
          <td>${avg.toFixed(4)}</td>
          <td class="${distCls}">${cur?cur.toFixed(4):'—'}</td>
          <td class="${distCls}">${dist!==null?(dist>=0?'+':'')+dist.toFixed(2)+'%':'—'}</td>
          <td style="max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.62rem;color:var(--ink-ghost)" title="${dcaStr}">${dcaStr}</td>
          <td class="${flCls}">${flPnl!==null?(flPnl>=0?'+':'')+flPnl.toFixed(4):'—'}</td>
          <td><span class="badge badge-active">ACTIVE</span> <span style="font-size:0.58rem;color:var(--ink-ghost)">liq ${liqStr}</span></td>
        </tr>`;
      }).join('');
    } else {
      bb.innerHTML = '<tr><td colspan="9"><div class="empty-state">NO ACTIVE POSITIONS</div></td></tr>';
    }

  } catch(e) { console.error('refresh error', e); }
}

refresh();
setInterval(refresh, 5000);
window.addEventListener('resize', () => {
  if (lastData) drawChart(document.getElementById('equity-canvas'), lastData.trades_chrono || []);
});

// ──────────────────────────────────────────────────────────
// CONTROL HELPERS — tokenless fetch
// ──────────────────────────────────────────────────────────

// Helper: POST to control endpoint (no auth token required — localhost only)
async function control(path, body) {
  try {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
    if (!resp.ok) {
      const text = await resp.text();
      let msg = text || `${resp.status} error`;
      if (resp.status === 409) msg = text || 'Conflict — action not allowed in current state';
      console.warn('control error:', msg);
      return null;
    }
    return await resp.json();
  } catch (e) {
    console.warn('control network error:', e.message || e);
    return null;
  }
}

// Refresh control state
async function refreshControlState() {
  try {
    const r = await fetch('/api/control/state');
    if (!r.ok) return;
    const state = await r.json();
    // Pass state to topbar botctl handler
    if (typeof window._applyBotState === 'function') window._applyBotState(state.auto_running || false);
  } catch (e) {
    console.warn('control state fetch error', e);
  }
}

refreshControlState();
setInterval(refreshControlState, 3000);

// ── HAMBURGER NAV ──
(function() {
  const btn = document.getElementById('hb-btn');
  const dd  = document.getElementById('hb-dropdown');
  if (!btn || !dd) return;
  function hbOpen()  { dd.classList.add('hb-open');    btn.setAttribute('aria-expanded','true'); }
  function hbClose(refocus) { dd.classList.remove('hb-open'); btn.setAttribute('aria-expanded','false'); if (refocus) btn.focus(); }
  btn.addEventListener('click', function(e) { e.stopPropagation(); dd.classList.contains('hb-open') ? hbClose(true) : hbOpen(); });
  document.addEventListener('click', function(e) { if (dd.classList.contains('hb-open') && !dd.contains(e.target) && e.target !== btn) hbClose(false); });
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && dd.classList.contains('hb-open')) hbClose(true); });
})();

// ── TOPBAR BOT START/STOP ──
(function() {
  const startBtn  = document.getElementById('botctl-start');
  const stopBtn   = document.getElementById('botctl-stop');
  const statusTxt = document.getElementById('botctl-status-text');
  const msgEl     = document.getElementById('botctl-msg');
  const spinner   = document.getElementById('botctl-spinner');

  function setBotCtlBusy(busy) {
    if (startBtn)  startBtn.disabled  = busy;
    if (stopBtn)   stopBtn.disabled   = busy;
    if (spinner)   spinner.style.display = busy ? 'block' : 'none';
  }

  function showBotCtlMsg(txt, isErr) {
    if (!msgEl) return;
    msgEl.textContent = txt;
    msgEl.style.color = isErr ? 'var(--loss)' : 'var(--profit)';
    setTimeout(() => { msgEl.textContent = ''; }, 4000);
  }

  function applyBotState(auto_running) {
    if (!startBtn || !stopBtn || !statusTxt) return;
    if (auto_running) {
      statusTxt.textContent = 'BOT LIVE';
      statusTxt.style.color = 'var(--profit)';
      startBtn.style.display = 'none';
      stopBtn.style.display  = '';
    } else {
      statusTxt.textContent = 'BOT INACTIVE';
      statusTxt.style.color = 'var(--ink-ghost)';
      startBtn.style.display = '';
      stopBtn.style.display  = 'none';
    }
  }

  async function pollUntilFlip(wantRunning, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        const r = await fetch('/api/control/state');
        if (!r.ok) continue;
        const s = await r.json();
        if (!!s.auto_running === wantRunning) {
          applyBotState(s.auto_running);
          return true;
        }
      } catch(e) { /* retry */ }
    }
    return false;
  }

  if (startBtn) startBtn.addEventListener('click', async function() {
    setBotCtlBusy(true);
    const res = await control('/api/control/auto/start', null);
    if (!res) { setBotCtlBusy(false); return; }
    const ok = await pollUntilFlip(true, 20000);
    setBotCtlBusy(false);
    if (!ok) showBotCtlMsg('Timeout waiting for start', true);
  });

  if (stopBtn) stopBtn.addEventListener('click', async function() {
    setBotCtlBusy(true);
    const res = await control('/api/control/auto/stop', null);
    if (!res) { setBotCtlBusy(false); return; }
    const ok = await pollUntilFlip(false, 20000);
    setBotCtlBusy(false);
    if (!ok) showBotCtlMsg('Timeout waiting for stop', true);
  });

  // Export applyBotState so refreshControlState can call it
  window._applyBotState = applyBotState;
})();
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# CONTROL PLANE — token auth, supervisor helpers, and control routes.
#
# Control routes are open (no token required) — dashboard is localhost-only.
# ──────────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------

def require_token(f):
    """Auth dropped — dashboard is localhost-only; all control routes are open."""
    @wraps(f)
    def _inner(*args, **kwargs):
        return f(*args, **kwargs)
    return _inner


# ---------------------------------------------------------------------------
# PID / process helpers
# ---------------------------------------------------------------------------

def _pid_alive_dash(pid: int) -> bool:
    """Return True if process *pid* is alive (dashboard-local copy, no bot import)."""
    try:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle == 0:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _proc_running(pidfile: Path) -> bool:
    """Return True if the pidfile exists and the recorded pid is alive."""
    try:
        if not pidfile.exists():
            return False
        pid = int(pidfile.read_text().strip())
        return _pid_alive_dash(pid)
    except Exception:
        return False


def _stop_proc(pidfile: Path, label: str):
    """Terminate the process recorded in *pidfile*.  Noop if already stopped."""
    try:
        if not pidfile.exists():
            return
        pid = int(pidfile.read_text().strip())
        if not _pid_alive_dash(pid):
            pidfile.unlink(missing_ok=True)
            return
        if sys.platform == "win32":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGTERM)
        logger.info("Sent stop signal to %s (pid=%d)", label, pid)
    except Exception as e:
        logger.warning("stop_proc %s: %s", label, e)


def _start_auto():
    """Launch bot.py --loop in a detached subprocess.

    Writes bot.pid immediately from the spawned pid. bot.py itself only writes
    the pidfile late in startup (after client init + reconcile), so without this
    the _proc_running guard stays False for several seconds — long enough for a
    second Start click to launch a duplicate bot that double-adopts positions.
    """
    proc = subprocess.Popen(
        [sys.executable, str(_HERE / "bot.py"), "--loop"],
        cwd=str(_HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _BOT_PID.write_text(str(proc.pid))


def _start_manual():
    """Launch trade.py --loop in a detached subprocess. Writes trade.pid at
    launch to close the same start-race window as _start_auto()."""
    proc = subprocess.Popen(
        [sys.executable, str(_HERE / "trade.py"), "--loop"],
        cwd=str(_HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _TRADE_PID.write_text(str(proc.pid))


# ---------------------------------------------------------------------------
# Control routes
# ---------------------------------------------------------------------------

_BOT_PID   = _HERE / "bot.pid"
_TRADE_PID = _HERE / "trade.pid"
_BOT_CONTROL_PATH    = _HERE / "bot_control.json"
_MANUAL_CONTROL_PATH = _HERE / "manual_control.json"


@app.route("/api/control/state")
def control_state():
    """GET — no token required (read-only status)."""
    auto_running   = _proc_running(_BOT_PID)
    manual_running = _proc_running(_TRADE_PID)
    if auto_running:
        mode = "auto"
    elif manual_running:
        mode = "manual"
    else:
        mode = "idle"
    return jsonify({
        "auto_running":   auto_running,
        "manual_running": manual_running,
        "mode":           mode,
    })


@app.route("/api/control/auto/start", methods=["POST"])
@require_token
def control_auto_start():
    if _proc_running(_TRADE_PID):
        return jsonify({"ok": False, "error": "manual engine is running; stop it first"}), 409
    if _proc_running(_BOT_PID):
        return jsonify({"ok": True, "note": "already running"})
    _start_auto()
    return jsonify({"ok": True})


@app.route("/api/control/auto/stop", methods=["POST"])
@require_token
def control_auto_stop():
    _stop_proc(_BOT_PID, "bot.py")
    return jsonify({"ok": True})


@app.route("/api/control/manual/start", methods=["POST"])
@require_token
def control_manual_start():
    if _proc_running(_BOT_PID):
        return jsonify({"ok": False, "error": "auto engine is running; stop it first"}), 409
    if _proc_running(_TRADE_PID):
        return jsonify({"ok": True, "note": "already running"})
    _start_manual()
    return jsonify({"ok": True})


@app.route("/api/control/manual/stop", methods=["POST"])
@require_token
def control_manual_stop():
    _stop_proc(_TRADE_PID, "trade.py")
    return jsonify({"ok": True})


@app.route("/api/control/manual/enter", methods=["POST"])
@require_token
def control_manual_enter():
    """Write a manual enter request to manual_control.json."""
    if not _proc_running(_TRADE_PID):
        return jsonify({"ok": False, "error": "manual engine is not running"}), 409
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).strip().upper()
    signal = str(body.get("signal", "")).strip()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required"}), 400
    if signal != "go":
        return jsonify({"ok": False, "error": "signal must be 'go'"}), 400
    try:
        with open(_MANUAL_CONTROL_PATH, "w") as f:
            json.dump({"enter": {"symbol": symbol, "signal": "go"}}, f)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "symbol": symbol})



# ──────────────────────────────────────────────────────────────────────────────
# MANUAL TRADING PAGE
# ──────────────────────────────────────────────────────────────────────────────
MANUAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCA Engine — Manual Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ── Z-INDEX SCALE ── */
:root {
  --z-nav: 100;
  --z-dropdown: 9999;
}
/* ── TOKENS (mirror dashboard) ── */
:root {
  --bg-base:    #0d0f12;
  --bg-surface: #131619;
  --bg-border:  #1e2228;
  --ink-primary:#e8eaf0;
  --ink-muted:  #8a90a0;
  --ink-ghost:  #484f5e;
  --accent:     #5b7fff;
  --profit:     #00c850;
  --loss:       #e63c3c;
  --amber:      #f5a623;
  --font-mono:  'JetBrains Mono', 'Menlo', monospace;
  --font-sans:  'Manrope', 'Inter', sans-serif;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { background: var(--bg-base); color: var(--ink-primary); font-family: var(--font-sans); font-size: 14px; }
body { min-height: 100vh; background: var(--bg-base); }

/* ── TOPBAR ── */
.topbar {
  position: sticky; top: 0; z-index: var(--z-nav);
  display: flex; align-items: center; gap: 0;
  height: 48px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--bg-border);
}
.topbar-brand {
  display: flex; align-items: center;
  padding: 0 20px; height: 100%;
  border-right: 1px solid var(--bg-border);
  font-family: var(--font-mono);
  font-size: 0.72rem; font-weight: 600;
  color: var(--ink-primary); text-decoration: none;
  letter-spacing: 0.04em; flex-shrink: 0;
}
.topbar-brand span { color: var(--accent); }
.topbar-title {
  padding: 0 18px; height: 100%;
  display: flex; align-items: center;
  font-family: var(--font-mono); font-size: 0.68rem;
  letter-spacing: 0.06em; color: var(--ink-ghost);
  border-right: 1px solid var(--bg-border);
}
.topbar-spacer { flex: 1; }

/* ── HAMBURGER NAV ── */
.hb-wrap { position: relative; display: flex; align-items: center; }
.hb-btn {
  display: flex; align-items: center; justify-content: center;
  width: 44px; height: 44px;
  background: none; border: 1px solid transparent; border-radius: 6px;
  color: var(--ink-muted); cursor: pointer;
  transition: color 0.15s, border-color 0.15s; flex-shrink: 0;
}
.hb-btn:hover { color: var(--ink-primary); border-color: var(--bg-border); }
.hb-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; color: var(--ink-primary); border-color: var(--bg-border); }
.hb-dropdown {
  position: fixed; top: 56px; right: 8px;
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 8px; padding: 4px; min-width: 160px;
  z-index: var(--z-dropdown);
  box-shadow: 0 8px 24px rgba(0,0,0,0.55);
  opacity: 0; transform: translateY(-6px); pointer-events: none;
  transition: opacity 0.18s ease-out, transform 0.18s ease-out;
}
@media (prefers-reduced-motion: reduce) { .hb-dropdown { transition: none; } }
.hb-dropdown.hb-open { opacity: 1; transform: translateY(0); pointer-events: auto; }
.hb-item {
  display: block; padding: 10px 14px;
  font-family: var(--font-mono); font-size: 0.72rem;
  letter-spacing: 0.04em; color: var(--ink-primary);
  text-decoration: none; border-radius: 5px;
  transition: background 0.12s, color 0.12s;
}
.hb-item:hover { background: var(--bg-border); color: var(--ink-primary); }
.hb-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; background: var(--bg-border); color: var(--ink-primary); }
.hb-active { color: var(--accent) !important; }

/* ── LAYOUT ── */
.manual-layout {
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 0;
  height: calc(100vh - 48px);
}
.manual-sidebar {
  background: var(--bg-surface);
  border-right: 1px solid var(--bg-border);
  overflow-y: auto;
  display: flex; flex-direction: column; gap: 0;
}
.manual-chart-area {
  display: flex; flex-direction: column;
  background: var(--bg-base);
  min-height: 0;
}

/* ── PANEL SECTIONS ── */
.panel-section {
  padding: 18px 20px;
  border-bottom: 1px solid var(--bg-border);
}
.panel-section-title {
  font-family: var(--font-mono);
  font-size: 0.6rem; font-weight: 600;
  letter-spacing: 0.1em; color: var(--ink-muted);
  text-transform: uppercase; margin-bottom: 14px;
}

/* ── STATE BADGE ── */
.state-row {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 14px;
}
.state-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  background: var(--ink-ghost);
}
.state-dot.dot-active { background: var(--profit); box-shadow: 0 0 6px var(--profit); }
.state-dot.dot-manual { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.state-label {
  font-family: var(--font-mono); font-size: 0.68rem;
  font-weight: 600; letter-spacing: 0.06em; color: var(--ink-primary);
}


/* ── CONTROL BUTTONS ── */
.ctrl-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.ctrl-btn {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 44px; padding: 0 16px;
  font-family: var(--font-mono); font-size: 0.66rem;
  font-weight: 600; letter-spacing: 0.06em;
  border-radius: 6px; border: 1px solid; cursor: pointer;
  transition: opacity 0.15s, background 0.15s; flex: 1;
  justify-content: center;
}
.ctrl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.ctrl-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.btn-start-manual {
  background: rgba(91,127,255,0.12); border-color: var(--accent); color: var(--accent);
}
.btn-start-manual:not(:disabled):hover { background: rgba(91,127,255,0.22); }
.btn-stop-manual {
  background: rgba(230,60,60,0.12); border-color: var(--loss); color: var(--loss);
}
.btn-stop-manual:not(:disabled):hover { background: rgba(230,60,60,0.22); }

/* ── SYMBOL SEARCH DROPDOWN ── */
.sym-picker { position: relative; width: 100%; }
.sym-search-input {
  background: var(--bg-base); border: 1px solid var(--bg-border);
  border-radius: 5px; padding: 9px 32px 9px 10px;
  font-family: var(--font-mono); font-size: 0.72rem;
  color: var(--ink-primary); outline: none; width: 100%;
  transition: border-color 0.14s; cursor: pointer;
}
.sym-search-input::placeholder { color: var(--ink-muted); opacity: 1; }
.sym-search-input:focus { border-color: var(--accent); }
.sym-search-chevron {
  position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
  pointer-events: none; color: var(--ink-ghost);
}
.sym-dropdown {
  position: fixed;
  background: var(--bg-surface);
  border: 1px solid var(--bg-border);
  border-radius: 6px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  z-index: var(--z-dropdown);
  overflow: hidden;
  display: none;
}
.sym-dropdown.open { display: block; }
.sym-dropdown-list {
  list-style: none;
  max-height: 280px; overflow-y: auto;
  overscroll-behavior: contain;
}
.sym-dropdown-list li {
  padding: 0 14px;
  font-family: var(--font-mono); font-size: 0.7rem;
  color: var(--ink-primary); cursor: pointer;
  min-height: 44px; display: flex; align-items: center;
  transition: background 0.1s;
}
.sym-dropdown-list li:hover { background: var(--bg-border); }
.sym-dropdown-list li:focus { background: var(--bg-border); outline: none; }
.sym-dropdown-list li:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.sym-dropdown-list li.sym-active { color: var(--accent); background: rgba(91,127,255,0.08); }
.sym-dropdown-list li.sym-kbd-focus { background: var(--bg-border); }
.sym-dropdown-empty {
  padding: 12px 14px;
  font-family: var(--font-mono); font-size: 0.68rem;
  color: var(--ink-ghost);
}
.sym-selected-display {
  font-family: var(--font-mono); font-size: 0.68rem;
  color: var(--ink-muted); margin-top: 8px; min-height: 18px;
}
.sym-selected-display strong { color: var(--accent); }

/* ── GO BUTTON ── */
.go-btn {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  width: 100%; min-height: 48px; margin-top: 4px;
  font-family: var(--font-mono); font-size: 0.78rem; font-weight: 700;
  letter-spacing: 0.08em;
  background: rgba(0,200,80,0.14); border: 1px solid var(--profit);
  color: var(--profit); border-radius: 7px; cursor: pointer;
  transition: background 0.15s, opacity 0.15s;
}
.go-btn:not(:disabled):hover { background: rgba(0,200,80,0.26); }
.go-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.go-btn:focus-visible { outline: 2px solid var(--profit); outline-offset: 2px; }
.go-spinner {
  width: 16px; height: 16px; color: var(--profit);
  display: none; flex-shrink: 0;
}
@media (prefers-reduced-motion: reduce) { .go-spinner circle { animation: none; stroke-dasharray: 56 0; } }

/* ── STATUS MSG ── */
.status-msg {
  font-family: var(--font-mono); font-size: 0.62rem;
  min-height: 18px; margin-top: 8px;
  color: var(--ink-ghost);
}

/* ── CHART AREA ── */
.chart-wrap {
  flex: 1; min-height: 0;
  position: relative;
}
#tv-chart-container {
  position: absolute; inset: 0;
}
.chart-fallback {
  display: none;
  flex-direction: column; align-items: center; justify-content: center;
  gap: 12px;
  position: absolute; inset: 0;
  font-family: var(--font-mono); font-size: 0.72rem;
  color: var(--ink-ghost); text-align: center;
}
.chart-fallback a { color: var(--accent); }
.chart-label {
  padding: 10px 20px;
  border-bottom: 1px solid var(--bg-border);
  font-family: var(--font-mono); font-size: 0.6rem;
  letter-spacing: 0.08em; color: var(--ink-ghost);
  display: flex; align-items: center; gap: 10px;
  flex-shrink: 0;
}
.chart-sym { color: var(--ink-primary); font-weight: 600; font-size: 0.72rem; }

/* ── RESPONSIVE ── */
@media (max-width: 800px) {
  .manual-layout { grid-template-columns: 1fr; grid-template-rows: auto 1fr; height: auto; }
  .manual-chart-area { min-height: 360px; }
}
</style>
</head>
<body>

<header class="topbar">
  <a href="/" class="topbar-brand">DCA<span>//</span>ENGINE</a>
  <div class="topbar-title">MANUAL TRADING</div>
  <div class="topbar-spacer"></div>
  <div class="hb-wrap">
    <button class="hb-btn" id="hb-btn" aria-label="Menu" aria-expanded="false" aria-haspopup="true">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <rect x="2" y="5"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="9"  width="16" height="2" rx="1" fill="currentColor"/>
        <rect x="2" y="13" width="16" height="2" rx="1" fill="currentColor"/>
      </svg>
    </button>
    <div class="hb-dropdown" id="hb-dropdown" role="menu">
      <a href="/"          class="hb-item"            role="menuitem">Home</a>
      <a href="/dashboard" class="hb-item"            role="menuitem">Dashboard</a>
      <a href="/manual"    class="hb-item hb-active"  role="menuitem">Manual</a>
    </div>
  </div>
</header>

<div class="manual-layout">

  <!-- ── SIDEBAR ── -->
  <aside class="manual-sidebar">

    <!-- Mode state -->
    <div class="panel-section">
      <div class="panel-section-title">Engine State</div>
      <div class="state-row">
        <span class="state-dot" id="m-state-dot"></span>
        <span class="state-label" id="m-state-label">LOADING…</span>
      </div>
      <div id="m-auto-warn" style="display:none;font-family:var(--font-mono);font-size:0.6rem;color:var(--amber);margin-bottom:10px;">
        Auto bot is running — stop it before starting manual.
      </div>
      <div class="ctrl-btns">
        <button class="ctrl-btn btn-start-manual" id="m-start-btn" disabled>
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><polygon points="1,0.5 10.5,5.5 1,10.5" fill="currentColor"/></svg>
          Start Manual
        </button>
        <button class="ctrl-btn btn-stop-manual" id="m-stop-btn" disabled>
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><rect x="1" y="1" width="9" height="9" rx="1" fill="currentColor"/></svg>
          Stop Manual
        </button>
      </div>
      <div class="status-msg" id="m-ctrl-msg"></div>
    </div>

    <!-- Symbol picker -->
    <div class="panel-section">
      <div class="panel-section-title">Symbol</div>
      <div class="sym-picker" id="sym-picker">
        <input type="text" id="m-sym-search" class="sym-search-input"
          placeholder="Search or select symbol…"
          autocomplete="off" aria-label="Search symbols"
          aria-haspopup="listbox" aria-expanded="false" aria-autocomplete="list"
          role="combobox" aria-controls="m-sym-dropdown-list">
        <svg class="sym-search-chevron" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M2 4l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div class="sym-dropdown" id="m-sym-dropdown" role="listbox" aria-label="Perpetual symbols">
          <ul class="sym-dropdown-list" id="m-sym-dropdown-list"></ul>
        </div>
      </div>
      <div class="sym-selected-display" id="m-sym-selected"></div>
    </div>

    <!-- GO button -->
    <div class="panel-section">
      <div class="panel-section-title">Trade Entry</div>
      <button class="go-btn" id="m-go-btn" disabled>
        <svg class="go-spinner" id="m-go-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" stroke-dasharray="28 56" stroke-linecap="round">
            <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/>
          </circle>
        </svg>
        GO — ENTER NOW
      </button>
      <div class="status-msg" id="m-go-msg"></div>
    </div>

  </aside>

  <!-- ── CHART ── -->
  <div class="manual-chart-area">
    <div class="chart-label">
      CHART&nbsp;·&nbsp;<span class="chart-sym" id="chart-sym-label">—</span>
      <span style="color:var(--ink-ghost);font-size:0.58rem;">Binance USDT-M Perpetual · Advanced Chart</span>
    </div>
    <div class="chart-wrap">
      <div id="tv-chart-container"></div>
      <div class="chart-fallback" id="chart-fallback">
        <div>TradingView chart unavailable.</div>
        <a href="https://www.binance.com/en/futures" target="_blank" rel="noopener noreferrer">Open Binance Futures ↗</a>
      </div>
    </div>
  </div>

</div>

<script>
// ── Control helper (tokenless — localhost only) ──
async function mControl(path, body, msgId) {
  const target = msgId || 'm-ctrl-msg';
  try {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
    if (!resp.ok) {
      const text = await resp.text();
      let msg = resp.status === 409 ? (text || 'Conflict — action not allowed in current state')
              : (text || `${resp.status} error`);
      mShowMsg(target, msg, true);
      return null;
    }
    return await resp.json();
  } catch(e) {
    mShowMsg(target, 'Network error: ' + (e.message || e), true);
    return null;
  }
}

function mShowMsg(id, txt, isErr) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = txt;
  el.style.color = isErr ? 'var(--loss)' : 'var(--profit)';
  setTimeout(() => { el.textContent = ''; }, 5000);
}

// ── State ──
let mState = { auto_running: false, manual_running: false, mode: 'IDLE', paused: false };
let mSelectedSymbol = '';

function mApplyState(state) {
  mState = state;
  const dot   = document.getElementById('m-state-dot');
  const label = document.getElementById('m-state-label');
  const warn  = document.getElementById('m-auto-warn');
  const startBtn = document.getElementById('m-start-btn');
  const stopBtn  = document.getElementById('m-stop-btn');

  if (state.auto_running) {
    dot.className = 'state-dot dot-active';
    label.textContent = 'AUTO RUNNING';
    warn.style.display = '';
    if (startBtn) { startBtn.disabled = true;  startBtn.title = 'Stop the auto bot first'; }
    if (stopBtn)  { stopBtn.disabled  = true;  stopBtn.title  = 'Auto bot is active — use the dashboard STOP'; }
  } else if (state.manual_running) {
    dot.className = 'state-dot dot-manual';
    label.textContent = 'MANUAL RUNNING';
    warn.style.display = 'none';
    if (startBtn) { startBtn.disabled = true;  startBtn.title = 'Manual engine already running'; }
    if (stopBtn)  { stopBtn.disabled  = false; stopBtn.title  = ''; }
  } else {
    dot.className = 'state-dot';
    label.textContent = 'IDLE';
    warn.style.display = 'none';
    if (startBtn) { startBtn.disabled = false; startBtn.title = ''; }
    if (stopBtn)  { stopBtn.disabled  = true;  stopBtn.title  = 'No manual session active'; }
  }
  mUpdateGoBtn();
}

function mUpdateGoBtn() {
  const btn = document.getElementById('m-go-btn');
  if (!btn) return;
  const ready = mState.manual_running && mSelectedSymbol;
  btn.disabled = !ready;
  if (!mState.manual_running) btn.title = 'Start the manual engine first';
  else if (!mSelectedSymbol)  btn.title = 'Select a symbol first';
  else                        btn.title = '';
}

async function mRefreshState() {
  try {
    const r = await fetch('/api/control/state');
    if (r.ok) mApplyState(await r.json());
  } catch(e) { /* ignore */ }
}
mRefreshState();
setInterval(mRefreshState, 3000);

// ── Start / Stop manual ──
document.getElementById('m-start-btn').addEventListener('click', async function() {
  this.disabled = true;
  const r = await mControl('/api/control/manual/start', null);
  if (r) { mShowMsg('m-ctrl-msg', 'Manual engine started', false); await mRefreshState(); }
  else this.disabled = false;
});
document.getElementById('m-stop-btn').addEventListener('click', async function() {
  this.disabled = true;
  const r = await mControl('/api/control/manual/stop', null);
  if (r) { mShowMsg('m-ctrl-msg', 'Manual engine stopped', false); await mRefreshState(); }
  else this.disabled = false;
});

// ── Symbol search dropdown ──
let mAllSymbols = [];
let mKbdIndex = -1;

const symSearch   = document.getElementById('m-sym-search');
const symDropdown = document.getElementById('m-sym-dropdown');
const symList     = document.getElementById('m-sym-dropdown-list');

function symOpen() {
  const rect = symSearch.getBoundingClientRect();
  symDropdown.style.left   = rect.left + 'px';
  symDropdown.style.top    = (rect.bottom + 2) + 'px';
  symDropdown.style.width  = rect.width + 'px';
  symDropdown.classList.add('open');
  symSearch.setAttribute('aria-expanded', 'true');
  mKbdIndex = -1;
}

function symClose() {
  symDropdown.classList.remove('open');
  symSearch.setAttribute('aria-expanded', 'false');
  mKbdIndex = -1;
}

function symToggle() {
  if (symDropdown.classList.contains('open')) symClose();
  else { mRenderDropdown(mAllSymbols); symOpen(); }
}

function mRenderDropdown(syms) {
  if (!syms.length) {
    symList.innerHTML = '<li class="sym-dropdown-empty">No symbols match</li>';
    return;
  }
  symList.innerHTML = syms.map(s =>
    `<li role="option" tabindex="-1" data-sym="${s}"${s === mSelectedSymbol ? ' class="sym-active"' : ''}>${s}</li>`
  ).join('');
  symList.querySelectorAll('li[data-sym]').forEach(li => {
    li.addEventListener('mousedown', e => { e.preventDefault(); mSelectSymbol(li.dataset.sym); });
  });
}

symSearch.addEventListener('click', function() {
  const q = this.value.trim().toUpperCase();
  mRenderDropdown(q ? mAllSymbols.filter(s => s.includes(q)) : mAllSymbols);
  symOpen();
});
symSearch.addEventListener('focus', function() {
  const q = this.value.trim().toUpperCase();
  mRenderDropdown(q ? mAllSymbols.filter(s => s.includes(q)) : mAllSymbols);
  symOpen();
});
symSearch.addEventListener('input', function() {
  const q = this.value.trim().toUpperCase();
  const filtered = q ? mAllSymbols.filter(s => s.includes(q)) : mAllSymbols;
  mRenderDropdown(filtered);
  if (!symDropdown.classList.contains('open')) symOpen();
  mKbdIndex = -1;
});
symSearch.addEventListener('keydown', function(e) {
  const items = symList.querySelectorAll('li[data-sym]');
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (!symDropdown.classList.contains('open')) { mRenderDropdown(mAllSymbols); symOpen(); }
    mKbdIndex = Math.min(mKbdIndex + 1, items.length - 1);
    items.forEach((li, i) => li.classList.toggle('sym-kbd-focus', i === mKbdIndex));
    if (items[mKbdIndex]) items[mKbdIndex].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    mKbdIndex = Math.max(mKbdIndex - 1, 0);
    items.forEach((li, i) => li.classList.toggle('sym-kbd-focus', i === mKbdIndex));
    if (items[mKbdIndex]) items[mKbdIndex].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (mKbdIndex >= 0 && items[mKbdIndex]) mSelectSymbol(items[mKbdIndex].dataset.sym);
    else if (items.length === 1) mSelectSymbol(items[0].dataset.sym);
  } else if (e.key === 'Escape') {
    symClose();
    this.blur();
  }
});
document.addEventListener('click', function(e) {
  if (!document.getElementById('sym-picker').contains(e.target) && !symDropdown.contains(e.target)) symClose();
});
window.addEventListener('resize', function() {
  if (symDropdown.classList.contains('open')) {
    const rect = symSearch.getBoundingClientRect();
    symDropdown.style.left  = rect.left + 'px';
    symDropdown.style.top   = (rect.bottom + 2) + 'px';
    symDropdown.style.width = rect.width + 'px';
  }
});

function mSelectSymbol(sym) {
  mSelectedSymbol = sym;
  symSearch.value = sym;
  symClose();
  const lbl = document.getElementById('m-sym-selected');
  if (lbl) lbl.innerHTML = 'Selected: <strong>' + sym + '</strong>';
  document.getElementById('chart-sym-label').textContent = sym;
  mUpdateGoBtn();
  mLoadChart(sym);
}

async function mLoadSymbols() {
  symList.innerHTML = '<li class="sym-dropdown-empty">Loading symbols…</li>';
  try {
    const r = await fetch('/api/symbols/perps');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    mAllSymbols = d.symbols || [];
    mRenderDropdown(mAllSymbols);
  } catch(e) {
    symList.innerHTML = '<li class="sym-dropdown-empty" style="color:var(--loss);">Failed to load — <button onclick="mLoadSymbols()" style="background:none;border:none;color:var(--accent);cursor:pointer;font-family:var(--font-mono);font-size:0.68rem;padding:0;">retry</button></li>';
  }
}
mLoadSymbols();

// ── TradingView chart ──
let tvWidget = null;
let tvScriptLoaded = false;
let tvScriptLoading = false;

function mLoadChart(sym) {
  const container = document.getElementById('tv-chart-container');
  const fallback  = document.getElementById('chart-fallback');
  if (!container) return;
  container.innerHTML = '';
  if (fallback) fallback.style.display = 'none';

  const tvSym = 'BINANCE:' + sym + '.P';

  function buildWidget() {
    try {
      tvWidget = new TradingView.widget({
        container_id: 'tv-chart-container',
        autosize: true,
        symbol: tvSym,
        interval: '60',
        timezone: 'Etc/UTC',
        theme: 'dark',
        style: '1',
        locale: 'en',
        toolbar_bg: '#131619',
        enable_publishing: false,
        hide_top_toolbar: false,
        hide_legend: false,
        save_image: false,
        studies: [],
      });
    } catch(e) {
      if (fallback) fallback.style.display = 'flex';
    }
  }

  if (tvScriptLoaded) {
    buildWidget();
    return;
  }
  if (tvScriptLoading) {
    // Queue: check every 200ms until loaded
    const poll = setInterval(() => {
      if (tvScriptLoaded) { clearInterval(poll); buildWidget(); }
    }, 200);
    return;
  }

  tvScriptLoading = true;
  const script = document.createElement('script');
  script.src = 'https://s3.tradingview.com/tv.js';
  script.onload  = () => { tvScriptLoaded = true; buildWidget(); };
  script.onerror = () => {
    tvScriptLoading = false;
    if (fallback) fallback.style.display = 'flex';
  };
  document.head.appendChild(script);
}

// ── GO button ──
document.getElementById('m-go-btn').addEventListener('click', async function() {
  if (!mSelectedSymbol) return;
  if (!confirm('LIVE REAL-MONEY TRADE\n\nThis will open a live position on ' + mSelectedSymbol + ' using real funds.\n\nContinue?')) return;
  const spinner = document.getElementById('m-go-spinner');
  this.disabled = true;
  if (spinner) spinner.style.display = 'block';
  const r = await mControl('/api/control/manual/enter', { symbol: mSelectedSymbol, signal: 'go' }, 'm-go-msg');
  if (spinner) spinner.style.display = 'none';
  if (r) {
    mShowMsg('m-go-msg', 'Trade entered on ' + mSelectedSymbol, false);
    await mRefreshState();
  } else {
    mUpdateGoBtn();
  }
});

// ── Hamburger nav ──
(function() {
  const btn = document.getElementById('hb-btn');
  const dd  = document.getElementById('hb-dropdown');
  if (!btn || !dd) return;
  function hbOpen()  { dd.classList.add('hb-open');    btn.setAttribute('aria-expanded','true'); }
  function hbClose(refocus) { dd.classList.remove('hb-open'); btn.setAttribute('aria-expanded','false'); if (refocus) btn.focus(); }
  btn.addEventListener('click', function(e) { e.stopPropagation(); dd.classList.contains('hb-open') ? hbClose(true) : hbOpen(); });
  document.addEventListener('click', function(e) { if (dd.classList.contains('hb-open') && !dd.contains(e.target) && e.target !== btn) hbClose(false); });
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && dd.classList.contains('hb-open')) hbClose(true); });
})();
</script>
</body>
</html>"""


# ── Flask routes ──

@app.route("/")
def index():
    return render_template_string(LANDING_HTML)


@app.route("/dashboard")
def dashboard():
    return render_template_string(HTML)


@app.route("/manual")
def manual():
    return render_template_string(MANUAL_HTML)


@app.route("/api/symbols/perps")
def api_symbols_perps():
    """Return list of USDT-M perpetual symbols. No token required (read-only)."""
    try:
        from perps import fetch_usdt_perps
        syms = fetch_usdt_perps() or []
    except Exception:
        # Fallback: use configured symbols from cache/bot_status
        with _cache_lock:
            raw = _cache.get("symbols", [])
        syms = [s["name"] if isinstance(s, dict) else str(s) for s in raw] if raw else []
    return jsonify({"symbols": syms})


@app.route("/api/data")
def api_data():
    with _cache_lock:
        return jsonify(_cache)


# ── Data pipeline ──

def _read_trades() -> list:
    path = _HERE / "bot_trade_log.csv"
    if not path.exists():
        return []
    with open(path) as f:
        reader = csv.DictReader(f)
        return list(reader)


from binance.client import Client as _BinanceClient
_BINANCE_CLIENT = None

def _get_binance_client():
    global _BINANCE_CLIENT
    if _BINANCE_CLIENT is None:
        key = os.environ.get("BINANCE_API_KEY", "")
        secret = os.environ.get("BINANCE_API_SECRET", "")
        if key and secret:
            _BINANCE_CLIENT = _BinanceClient(key, secret, requests_params={"timeout": 30})
    return _BINANCE_CLIENT

def _bot_alive() -> bool:
    """Check if bot.py is running by pinging Binance account status (bot keeps connection alive)."""
    try:
        import subprocess
        if sys.platform == "win32":
            r = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 'Get-CimInstance Win32_Process | Where-Object { $_.Name -like "*python*" } | Select-Object CommandLine | Format-Table -HideTableHeaders'],
                capture_output=True, text=True, timeout=5,
            )
            return "bot.py" in r.stdout.lower()
        else:
            r = subprocess.run(
                ["pgrep", "-f", "bot.py"], capture_output=True, timeout=5
            )
            return r.returncode == 0
    except Exception:
        return False


def _read_bot_status() -> dict:
    path = _HERE / "bot_status.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _fetch_baskets() -> list:
    """Fetch active positions from bot status file or Binance Futures."""
    # Try reading from bot status file first
    try:
        import json
        from pathlib import Path
        bot_status_path = Path(__file__).parent / "bot_status.json"
        if bot_status_path.exists():
            with open(bot_status_path) as f:
                status = json.load(f)
                baskets = status.get("baskets", [])
                if baskets:
                    return baskets
    except Exception:
        pass

    # Fall back to fetching from Binance API
    try:
        from binance.client import Client as BinanceClient
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            return []
        client = BinanceClient(api_key, api_secret, requests_params={"timeout": 30})

        positions = []
        for attempt in range(3):
            try:
                positions = client.futures_account()["positions"]
                break
            except Exception:
                if attempt == 2:
                    return []
                import time
                time.sleep(0.5)
        active = []
        for p in positions:
            amt = float(p["positionAmt"])
            if abs(amt) < 1e-9:
                continue
            sym = p["symbol"]
            entry = float(p["entryPrice"])
            mark = float(p["markPrice"])
            upnl = float(p["unRealizedProfit"])
            liq = float(p["liquidationPrice"])
            margin = float(p["isolatedWallet"] or p.get("positionInitialMargin", 0))

            # Build DCA level markers from open orders
            dca_levels = []
            try:
                orders = client.futures_get_open_orders(symbol=sym)
                for o in orders:
                    if o["side"] == "BUY" and o["type"] == "LIMIT":
                        dca_levels.append(float(o["price"]))
            except Exception:
                pass

            active.append({
                "symbol": sym,
                "avg_entry": entry,
                "current_price": mark,
                "floating_pnl": round(upnl, 4),
                "margin": round(abs(margin), 2) if margin else 0,
                "liquidation": liq,
                "state": "active",
                "entry_time": "",
                "dca_levels": sorted(dca_levels, reverse=True),
            })
        return active
    except Exception as e:
        logger.warning("Basket fetch: %s", e)
        return []


def _update_cache():
    trades = _read_trades()
    n = len(trades)
    wins = [t for t in trades if float(t.get("net_pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("net_pnl", 0)) < 0]
    wr = len(wins) / n if n > 0 else 0
    net = sum(float(t.get("net_pnl", 0)) for t in trades)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum(float(t.get("net_pnl", 0)) for t in trades if t.get("exit_time") and t.get("exit_time", "").replace(" ", "T").split("T")[0] == today)

    pnls = [float(t["net_pnl"]) for t in trades if t.get("net_pnl")]
    peak = 0.0
    running = 0.0
    for v in pnls:
        running += v
        if running > peak:
            peak = running
    best = max(pnls) if pnls else None
    worst = min(pnls) if pnls else None

    win_pnls = [float(t["net_pnl"]) for t in wins]
    loss_pnls = [abs(float(t["net_pnl"])) for t in losses]
    avg_w = sum(win_pnls) / len(win_pnls) if win_pnls else None
    avg_l = sum(loss_pnls) / len(loss_pnls) if loss_pnls else None

    # Breakeven win rate
    denom = (avg_w or 0) + (avg_l or 0)
    be = (avg_l or 0) / denom if denom > 0 else 0

    # Consecutive + last result
    consec = 0
    last_result = None
    for t in reversed(trades):
        p = float(t.get("net_pnl", 0))
        if p > 0:
            if last_result is None:
                last_result = "win"
            consec += 1
        elif p < 0:
            if last_result is None:
                last_result = "loss"
            consec += 1
        else:
            break

    recent = trades[-30:][::-1] if trades else []
    bot_status = _read_bot_status()

    with _cache_lock:
        _cache.update({
            "trades_chrono": trades,
            "recent_trades": recent,
            "total_trades": n,
            "win_rate": wr,
            "breakeven_winrate": be,
            "net_pnl": round(net, 4),
            "today_pnl": round(today_pnl, 4),
            "peak_pnl": round(peak, 4),
            "consecutive_wins": consec,
            "last_trade_result": last_result,
            "best_trade": round(best, 4) if best is not None else None,
            "worst_trade": round(worst, 4) if worst is not None else None,
            "avg_win": round(avg_w, 4) if avg_w is not None else None,
            "avg_loss": round(avg_l, 4) if avg_l is not None else None,
            "bot_alive": bool(bot_status.get("bot_alive", _bot_alive())),
            "balance": float(bot_status.get("balance", 0.0)),
            "symbols": bot_status.get("symbols", []),
            "baskets": _fetch_baskets(),
        })


def _poll_loop():
    while True:
        try:
            _update_cache()
        except Exception:
            pass
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="DCA Bot Dashboard")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    Thread(target=_poll_loop, daemon=True).start()
    print(f"DCA Bot: http://localhost:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
