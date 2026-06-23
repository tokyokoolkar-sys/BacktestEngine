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
    from flask import Flask, jsonify, render_template_string
except ImportError:
    print("Install flask: pip install flask")
    sys.exit(1)

_HERE = Path(__file__).parent
logger = logging.getLogger("dashboard")
app = Flask(__name__)
_cache = {}
_cache_lock = Lock()

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCA Bot — Instrumentation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-deep: #0b1119;
    --bg-panel: #111a26;
    --bg-card: #17212d;
    --bg-hover: #1c2838;
    --border: #1e2d3d;
    --text-primary: #e1e8f0;
    --text-secondary: #8899a8;
    --text-muted: #546677;
    --profit: #34d399;
    --loss: #fb7185;
    --amber: #f59e0b;
    --blue: #60a5fa;
    --chart-grid: #1a2940;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Space Grotesk', -apple-system, sans-serif;
    background: var(--bg-deep);
    color: var(--text-primary);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* ── Status bar ── */
  .status-bar {
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 14px 24px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    font-size: 0.8rem;
    flex-wrap: wrap;
  }
  .status-item { display: flex; align-items: center; gap: 8px; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    transition: background 0.5s;
  }
  .dot-on { background: var(--profit); box-shadow: 0 0 8px var(--profit); }
  .dot-off { background: var(--text-muted); }
  .dot-amber { background: var(--amber); box-shadow: 0 0 8px var(--amber); }
  .status-label { color: var(--text-secondary); }
  .status-value { font-family: 'JetBrains Mono', monospace; font-weight: 500; }
  .status-spacer { flex: 1; }
  .streak-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 20px;
    background: rgba(52, 211, 153, 0.1);
    border: 1px solid rgba(52, 211, 153, 0.2);
    font-size: 0.75rem; font-weight: 600;
  }
  .streak-badge.loss-bg { background: rgba(251, 113, 133, 0.1); border-color: rgba(251, 113, 133, 0.2); }
  .streak-count { color: var(--profit); font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; }
  .streak-badge.loss-bg .streak-count { color: var(--loss); }

  /* ── Hero: equity strip chart ── */
  .chart-section {
    padding: 20px 24px 16px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
  }
  .chart-header {
    display: flex; justify-content: space-between; align-items: flex-end;
    margin-bottom: 12px;
  }
  .chart-title {
    font-size: 0.65rem; letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--text-muted); font-weight: 600;
  }
  .chart-stats {
    display: flex; gap: 20px; font-family: 'JetBrains Mono', monospace;
  }
  .chart-stat { text-align: right; }
  .chart-stat-label {
    font-size: 0.6rem; text-transform: uppercase; letter-spacing: 1px;
    color: var(--text-muted); font-family: 'Space Grotesk', sans-serif;
  }
  .chart-stat-value { font-size: 1.1rem; font-weight: 600; }
  .chart-canvas-wrap {
    position: relative; width: 100%; height: 140px;
    background: var(--bg-card); border-radius: 8px;
    border: 1px solid var(--border); overflow: hidden;
  }
  .chart-canvas-wrap canvas {
    width: 100%; height: 100%; display: block;
  }
  .chart-empty {
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: var(--text-muted);
    font-size: 0.85rem; font-family: 'JetBrains Mono', monospace;
  }

  /* ── Metrics grid ── */
  .metrics {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .metric {
    background: var(--bg-panel);
    padding: 14px 16px;
    display: flex; flex-direction: column;
  }
  .metric-label {
    font-size: 0.6rem; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--text-muted); font-weight: 600; margin-bottom: 4px;
  }
  .metric-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.3rem; font-weight: 600; line-height: 1.2;
  }
  .metric-sub {
    font-size: 0.65rem; color: var(--text-secondary); margin-top: 2px;
    font-family: 'JetBrains Mono', monospace;
  }
  .text-profit { color: var(--profit); }
  .text-loss { color: var(--loss); }
  .text-amber { color: var(--amber); }
  .text-primary { color: var(--text-primary); }
  .text-muted-small { color: var(--text-muted); font-size: 0.65rem; }

  /* ── Table section ── */
  .tables {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
    background: var(--border); flex: 1;
  }
  .table-panel {
    background: var(--bg-panel); display: flex; flex-direction: column;
  }
  .table-header {
    padding: 12px 16px 8px;
    font-size: 0.6rem; letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--text-muted); font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  .table-wrap {
    overflow-x: auto; flex: 1;
  }
  table {
    width: 100%; border-collapse: collapse;
    font-family: 'JetBrains Mono', monospace; font-size: 0.7rem;
  }
  th {
    text-align: left; padding: 8px 10px;
    color: var(--text-muted); font-weight: 500;
    border-bottom: 1px solid var(--border);
    font-family: 'Space Grotesk', sans-serif;
    font-size: 0.6rem; letter-spacing: 1px; text-transform: uppercase;
  }
  td {
    padding: 7px 10px;
    border-bottom: 1px solid rgba(30, 45, 61, 0.6);
    transition: background 0.15s;
  }
  tr:hover td { background: var(--bg-hover); }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 0.6rem; font-weight: 600; font-family: 'Space Grotesk', sans-serif;
  }
  .badge-win { background: rgba(52, 211, 153, 0.12); color: var(--profit); }
  .badge-loss { background: rgba(251, 113, 133, 0.12); color: var(--loss); }
  .badge-active { background: rgba(96, 165, 250, 0.12); color: var(--blue); }
  .empty-state {
    padding: 32px; text-align: center; color: var(--text-muted);
    font-size: 0.8rem; font-family: 'Space Grotesk', sans-serif;
  }

  /* ── Responsive ── */
  @media (max-width: 900px) {
    .metrics { grid-template-columns: repeat(3, 1fr); }
    .tables { grid-template-columns: 1fr; }
    .status-bar { gap: 12px; padding: 10px 16px; }
    .chart-section { padding: 12px 16px; }
  }
  @media (max-width: 500px) {
    .metrics { grid-template-columns: repeat(2, 1fr); }
    .chart-canvas-wrap { height: 100px; }
    .status-bar { font-size: 0.7rem; }
  }

  /* ── Reduced motion ── */
  @media (prefers-reduced-motion: reduce) {
    .status-dot { transition: none; }
    canvas { transition: none; }
  }

  @media (prefers-color-scheme: light) {
    :root {
      --bg-deep: #f0f4f8;
      --bg-panel: #ffffff;
      --bg-card: #f8fafc;
      --bg-hover: #f1f5f9;
      --border: #dce4ec;
      --text-primary: #1a2332;
      --text-secondary: #546677;
      --text-muted: #8899a8;
    }
  }
</style>
</head>
<body>

<!-- ── Status bar ── -->
<div class="status-bar" id="status-bar">
  <div class="status-item">
    <span class="status-dot" id="bot-dot"></span>
    <span class="status-label">Bot</span>
    <span class="status-value" id="bot-status">—</span>
  </div>
  <div class="status-item">
    <span class="status-dot dot-amber" id="oracle-dot"></span>
    <span class="status-label">Oracle</span>
    <span class="status-value" id="oracle-status">—</span>
  </div>
  <div class="status-item">
    <span class="status-label">Balance</span>
    <span class="status-value" id="balance">—</span>
    <span class="status-label">USDT</span>
  </div>
  <div class="status-spacer"></div>
  <div class="status-item">
    <span class="status-label" id="streak-label">Consecutive</span>
    <span class="streak-badge" id="streak-badge">
      <span class="streak-count" id="streak-count">0</span>
      <span id="streak-word">wins</span>
    </span>
  </div>
</div>

<!-- ── Equity strip chart ── -->
<div class="chart-section">
  <div class="chart-header">
    <div class="chart-title">Equity Curve</div>
    <div class="chart-stats">
      <div class="chart-stat">
        <div class="chart-stat-label">Net PnL</div>
        <div class="chart-stat-value" id="net-pnl">—</div>
      </div>
      <div class="chart-stat">
        <div class="chart-stat-label">Today</div>
        <div class="chart-stat-value" id="today-pnl">—</div>
      </div>
      <div class="chart-stat">
        <div class="chart-stat-label">Peak</div>
        <div class="chart-stat-value" id="peak-pnl">—</div>
      </div>
    </div>
  </div>
  <div class="chart-canvas-wrap">
    <canvas id="equity-canvas"></canvas>
    <div class="chart-empty" id="chart-empty">No trades yet — waiting for first signal</div>
  </div>
</div>

<!-- ── Stats grid ── -->
<div class="metrics">
  <div class="metric">
    <div class="metric-label">Trades</div>
    <div class="metric-value text-primary" id="total-trades">0</div>
  </div>
  <div class="metric">
    <div class="metric-label">Win Rate</div>
    <div class="metric-value" id="win-rate">—</div>
    <div class="metric-sub" id="win-rate-sub"></div>
  </div>
  <div class="metric">
    <div class="metric-label">Avg Win</div>
    <div class="metric-value text-profit" id="avg-win">—</div>
  </div>
  <div class="metric">
    <div class="metric-label">Avg Loss</div>
    <div class="metric-value text-loss" id="avg-loss">—</div>
  </div>
  <div class="metric">
    <div class="metric-label">Best</div>
    <div class="metric-value text-profit" id="best-trade">—</div>
  </div>
  <div class="metric">
    <div class="metric-label">Worst</div>
    <div class="metric-value text-loss" id="worst-trade">—</div>
  </div>
</div>

<!-- ── Tables ── -->
<div class="tables">
  <div class="table-panel">
    <div class="table-header">Recent Trades</div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>Time</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>PnL</th><th>%</th><th>Exit</th><th>#S</th>
      </tr></thead><tbody id="trades-body">
        <tr><td colspan="8"><div class="empty-state">Waiting for first trade</div></td></tr>
      </tbody></table>
    </div>
  </div>
  <div class="table-panel">
    <div class="table-header">Active Baskets</div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>Symbol</th><th>Entry</th><th>Avg</th><th>Current</th><th>v Entry</th><th>DCA</th><th>Fl PnL</th><th>Status</th>
      </tr></thead><tbody id="baskets-body">
        <tr><td colspan="6"><div class="empty-state">No active positions</div></td></tr>
      </tbody></table>
    </div>
  </div>
</div>

<script>
// ── Strip chart ──
function drawChart(canvas, trades) {
  const wrap = canvas.parentElement;
  const empty = document.getElementById('chart-empty');
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;

  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  if (!trades || trades.length < 1) {
    empty.style.display = 'flex';
    return;
  }
  empty.style.display = 'none';

  const padL = 0, padR = 0, padT = 10, padB = 10;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  // Build equity curve
  let running = 0;
  const points = [{x: 0, y: 0}];
  trades.forEach((t, i) => {
    running += parseFloat(t.net_pnl || 0);
    points.push({x: i + 1, y: running});
  });

  const values = points.map(p => p.y);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0.01);
  const range = max - min || 0.01;

  const xScale = (range > 0 && points.length > 1)
    ? (i) => padL + (i / (points.length - 1)) * plotW
    : () => padL + plotW / 2;
  const yScale = (v) => padT + plotH - ((v - min) / range) * plotH;

  // Grid lines
  ctx.strokeStyle = getComputedStyle(canvas).getPropertyValue('--chart-grid').trim() || '#1a2940';
  ctx.lineWidth = 0.5;
  const gridCount = 4;
  for (let g = 0; g <= gridCount; g++) {
    const y = padT + (g / gridCount) * plotH;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
  }

  // Zero line if in view
  if (min < 0 && max > 0) {
    const zy = yScale(0);
    ctx.strokeStyle = 'rgba(84, 102, 119, 0.3)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, zy);
    ctx.lineTo(w - padR, zy);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Gradient fill
  const finalY = yScale(points[points.length - 1].y);
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  if (running >= 0) {
    grad.addColorStop(0, 'rgba(52, 211, 153, 0.25)');
    grad.addColorStop(0.5, 'rgba(52, 211, 153, 0.08)');
    grad.addColorStop(1, 'rgba(52, 211, 153, 0.01)');
  } else {
    grad.addColorStop(0, 'rgba(251, 113, 133, 0.25)');
    grad.addColorStop(0.5, 'rgba(251, 113, 133, 0.08)');
    grad.addColorStop(1, 'rgba(251, 113, 133, 0.01)');
  }

  ctx.beginPath();
  points.forEach((p, i) => { const px = xScale(i); const py = yScale(p.y); i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py); });
  ctx.lineTo(xScale(points.length - 1), padT + plotH);
  ctx.lineTo(xScale(0), padT + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  points.forEach((p, i) => { const px = xScale(i); const py = yScale(p.y); i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py); });
  ctx.strokeStyle = running >= 0 ? '#34d399' : '#fb7185';
  ctx.lineWidth = 2;
  ctx.stroke();

  // Glow
  ctx.beginPath();
  points.forEach((p, i) => { const px = xScale(i); const py = yScale(p.y); i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py); });
  ctx.strokeStyle = (running >= 0 ? 'rgba(52, 211, 153,' : 'rgba(251, 113, 133,') + '0.3)';
  ctx.lineWidth = 6;
  ctx.stroke();

  // Endpoint dot
  const last = points[points.length - 1];
  const lx = xScale(points.length - 1);
  const ly = yScale(last.y);
  ctx.beginPath();
  ctx.arc(lx, ly, 3, 0, Math.PI * 2);
  ctx.fillStyle = running >= 0 ? '#34d399' : '#fb7185';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(lx, ly, 5, 0, Math.PI * 2);
  ctx.strokeStyle = running >= 0 ? 'rgba(52, 211, 153, 0.4)' : 'rgba(251, 113, 133, 0.4)';
  ctx.lineWidth = 2;
  ctx.stroke();
}

// ── Data fetching ──
let lastData = null;

async function refresh() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    lastData = d;

    // ── Status bar ──
    document.getElementById('bot-dot').className = 'status-dot ' + (d.bot_alive ? 'dot-on' : 'dot-off');
    document.getElementById('bot-status').textContent = d.bot_alive ? 'Running' : 'Stopped';
    document.getElementById('oracle-dot').className = 'status-dot ' + (d.oracle_enabled ? 'dot-amber' : 'dot-off');
    document.getElementById('oracle-status').textContent = d.oracle_enabled ? 'Active' : 'Off';
    document.getElementById('balance').textContent = d.balance.toFixed(2);

    const consec = d.consecutive_wins || 0;
    const streakCount = document.getElementById('streak-count');
    const streakWord = document.getElementById('streak-word');
    const streakBadge = document.getElementById('streak-badge');
    const streakLabel = document.getElementById('streak-label');
    streakCount.textContent = consec;
    if (consec > 0 && d.last_trade_result === 'win') {
      streakWord.textContent = 'wins';
      streakBadge.className = 'streak-badge';
      streakLabel.textContent = 'Winning streak';
    } else if (consec > 0 && d.last_trade_result === 'loss') {
      streakWord.textContent = 'losses';
      streakBadge.className = 'streak-badge loss-bg';
      streakLabel.textContent = 'Losing streak';
    } else {
      streakWord.textContent = 'trades';
      streakBadge.className = 'streak-badge';
      streakLabel.textContent = 'Consecutive';
    }

    // ── Chart header stats ──
    const netPnl = d.net_pnl || 0;
    const netEl = document.getElementById('net-pnl');
    netEl.textContent = (netPnl >= 0 ? '+' : '') + netPnl.toFixed(4);
    netEl.className = 'chart-stat-value ' + (netPnl >= 0 ? 'text-profit' : 'text-loss');

    const todayEl = document.getElementById('today-pnl');
    todayEl.textContent = (d.today_pnl >= 0 ? '+' : '') + d.today_pnl.toFixed(4);
    todayEl.className = 'chart-stat-value ' + (d.today_pnl >= 0 ? 'text-profit' : 'text-loss');

    const peakEl = document.getElementById('peak-pnl');
    peakEl.textContent = (d.peak_pnl >= 0 ? '+' : '') + (d.peak_pnl || 0).toFixed(4);
    peakEl.className = 'chart-stat-value text-profit';

    // ── Chart ──
    const canvas = document.getElementById('equity-canvas');
    drawChart(canvas, d.trades_chrono || []);

    // ── Stats grid ──
    document.getElementById('total-trades').textContent = d.total_trades || 0;

    const wr = (d.win_rate || 0) * 100;
    const wrEl = document.getElementById('win-rate');
    wrEl.textContent = wr.toFixed(1) + '%';
    wrEl.className = 'metric-value ' + (wr >= 50 ? 'text-profit' : wr > 0 ? 'text-amber' : 'text-primary');
    const wrSub = document.getElementById('win-rate-sub');
    const be = d.breakeven_winrate || 0;
    wrSub.textContent = 'BE ' + (be * 100).toFixed(1) + '%';

    const aw = document.getElementById('avg-win');
    aw.textContent = d.avg_win ? '+' + d.avg_win.toFixed(4) : '—';
    const al = document.getElementById('avg-loss');
    al.textContent = d.avg_loss ? d.avg_loss.toFixed(4) : '—';
    document.getElementById('best-trade').textContent = d.best_trade ? '+' + d.best_trade.toFixed(4) : '—';
    document.getElementById('worst-trade').textContent = d.worst_trade ? d.worst_trade.toFixed(4) : '—';

    // ── Trades table ──
    const tb = document.getElementById('trades-body');
    const recent = d.recent_trades || [];
    if (recent.length) {
      tb.innerHTML = recent.map(t => {
        const pnl = parseFloat(t.net_pnl || 0);
        const isWin = pnl > 0;
        const pct = parseFloat(t.pnl_pct || 0);
        return `<tr>
          <td>${(t.exit_time || '').substr(11,8)}</td>
          <td>${t.symbol || ''}</td>
          <td>${parseFloat(t.entry_price || 0).toFixed(4)}</td>
          <td>${parseFloat(t.exit_price || 0).toFixed(4)}</td>
          <td class="${isWin ? 'text-profit' : 'text-loss'}">${isWin ? '+' : ''}${pnl.toFixed(4)}</td>
          <td class="${isWin ? 'text-profit' : 'text-loss'}">${isWin ? '+' : ''}${pct.toFixed(2)}</td>
          <td><span class="badge ${isWin ? 'badge-win' : 'badge-loss'}">${t.exit_reason || ''}</span></td>
          <td>${t.n_safety_filled || 0}</td>
        </tr>`;
      }).join('');
    } else {
      tb.innerHTML = '<tr><td colspan="8"><div class="empty-state">Waiting for first trade</div></td></tr>';
    }

    // ── Baskets table ──
    const bb = document.getElementById('baskets-body');
    const baskets = d.baskets || [];
    if (baskets.length) {
      bb.innerHTML = baskets.map(b => {
        const flPnl = b.floating_pnl != null ? parseFloat(b.floating_pnl) : null;
        const avg = parseFloat(b.avg_entry || 0);
        const cur = b.current_price ? parseFloat(b.current_price) : null;
        const dist = cur ? ((cur - avg) / avg * 100) : null;
        const dcaStr = b.dca_levels && b.dca_levels.length
          ? b.dca_levels.map(l => l.toFixed(4)).join(', ')
          : '—';
        const liqStr = b.liquidation ? parseFloat(b.liquidation).toFixed(4) : '—';
        return `<tr>
          <td>${b.symbol || ''}</td>
          <td>${b.entry_time ? b.entry_time.substr(11,8) : '—'}</td>
          <td>${avg.toFixed(4)}</td>
          <td class="${dist !== null && dist > 0 ? 'text-profit' : dist !== null && dist < 0 ? 'text-loss' : ''}">${cur ? cur.toFixed(4) : '—'}</td>
          <td class="${dist !== null && dist > 0 ? 'text-profit' : dist !== null && dist < 0 ? 'text-loss' : ''}">${dist !== null ? (dist >= 0 ? '+' : '') + dist.toFixed(2) + '%' : '—'}</td>
          <td style="font-size:0.6rem;color:var(--text-muted);max-width:100px;overflow:hidden;text-overflow:ellipsis;" title="${dcaStr}">${dcaStr}</td>
          <td class="${flPnl !== null && flPnl >= 0 ? 'text-profit' : flPnl !== null ? 'text-loss' : ''}">${flPnl !== null ? (flPnl >= 0 ? '+' : '') + flPnl.toFixed(4) : '—'}</td>
          <td><span class="badge badge-active">Active</span> <span style="font-size:0.55rem;color:var(--text-muted)">liq ${liqStr}</span></td>
        </tr>`;
      }).join('');
    } else {
      bb.innerHTML = '<tr><td colspan="8"><div class="empty-state">No active positions</div></td></tr>';
    }
  } catch(e) {
    console.error('refresh error', e);
  }
}

// ── Init ──
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
    """Fetch active positions and open orders from Binance Futures."""
    try:
        from binance.client import Client as BinanceClient
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            return []
        client = BinanceClient(api_key, api_secret)

        positions = client.futures_account()["positions"]
        active = []
        for p in positions:
            amt = float(p["positionAmt"])
            if abs(amt) < 0.0001:
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
    losses = [t for t in trades if float(t.get("net_pnl", 0)) <= 0]
    wr = len(wins) / n if n > 0 else 0
    net = sum(float(t.get("net_pnl", 0)) for t in trades)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum(float(t.get("net_pnl", 0)) for t in trades if t.get("exit_time", "").startswith(today))

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
