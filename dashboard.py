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
    from flask import Flask, jsonify, redirect, render_template_string, url_for
except ImportError:
    print("Install flask: pip install flask")
    sys.exit(1)

_HERE = Path(__file__).parent
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
  <a href="/dashboard" class="nav-link">→ PANEL</a>
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
  --z-base:    0;
  --z-overlay: 10;
  --z-nav:     20;
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
      <span class="topbar-dot" id="bot-dot"></span>
      <span class="topbar-item-label">BOT</span>
      <span class="topbar-item-value" id="bot-status">—</span>
    </div>
    <div class="topbar-item">
      <span class="topbar-dot dot-amber" id="oracle-dot"></span>
      <span class="topbar-item-label">ORACLE</span>
      <span class="topbar-item-value" id="oracle-status">—</span>
    </div>
    <div class="topbar-item">
      <span class="topbar-item-label">BALANCE</span>
      <span class="topbar-item-value" id="balance">—</span>
      <span class="topbar-item-label">USDT</span>
    </div>
    <div class="topbar-spacer"></div>
  </div>
  <div class="topbar-streak">
    <span class="streak-num" id="streak-num">0</span>
    <span class="streak-label" id="streak-label">streak</span>
  </div>
  <a href="/" class="topbar-back">← HOME</a>
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
          <th>Symbol</th><th>Entry@</th><th>Avg</th><th>Mark</th>
          <th>Δ Entry</th><th>DCA Levels</th><th>Float PnL</th><th>Status</th>
        </tr></thead>
        <tbody id="baskets-body">
          <tr><td colspan="8"><div class="empty-state">NO ACTIVE POSITIONS</div></td></tr>
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

    // Topbar
    const alive = d.bot_alive;
    const botDot = document.getElementById('bot-dot');
    botDot.className = 'topbar-dot ' + (alive ? 'dot-live' : 'dot-off');
    document.getElementById('bot-status').textContent = alive ? 'RUNNING' : 'STOPPED';
    const oracleDot = document.getElementById('oracle-dot');
    oracleDot.className = 'topbar-dot ' + (d.oracle_enabled ? 'dot-amber' : 'dot-off');
    document.getElementById('oracle-status').textContent = d.oracle_enabled ? 'ACTIVE' : 'OFF';
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
        return `<tr>
          <td>${b.symbol||''}</td>
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
      bb.innerHTML = '<tr><td colspan="8"><div class="empty-state">NO ACTIVE POSITIONS</div></td></tr>';
    }
  } catch(e) { console.error('refresh error', e); }
}

refresh();
setInterval(refresh, 5000);
window.addEventListener('resize', () => {
  if (lastData) drawChart(document.getElementById('equity-canvas'), lastData.trades_chrono || []);
});
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
            _BINANCE_CLIENT = _BinanceClient(key, secret)
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
        client = BinanceClient(api_key, api_secret)

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
            "oracle_enabled": bool(bot_status.get("oracle", False)),
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
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
