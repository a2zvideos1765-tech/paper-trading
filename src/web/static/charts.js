// Multi-series equity chart: strategy vs NIFTY 50 vs SENSEX.
// Theme-aware: rebuilds itself when a 'themechange' event fires (see theme.js).

(function () {
  function readThemeColors() {
    const cs = getComputedStyle(document.documentElement);
    return {
      text:   (cs.getPropertyValue('--text').trim()        || '#1f1f1f'),
      muted:  (cs.getPropertyValue('--text-muted').trim()  || '#5b6b7a'),
      grid:   (cs.getPropertyValue('--border').trim()      || 'rgba(0,0,0,0.08)'),
      accent: (cs.getPropertyValue('--accent').trim()      || '#ff5722'),
      up:     (cs.getPropertyValue('--up').trim()          || '#1ba94c'),
      down:   (cs.getPropertyValue('--down').trim()        || '#e94b35'),
    };
  }

  // Lightweight-charts renders its time axis in UTC. Our timestamps are IST
  // (00:00 IST daily points = 18:30 UTC the prior day), which would label the
  // axis a day early. Shift by +5:30 so the library's UTC rendering coincides
  // with the IST wall clock. Applied consistently for setData AND crosshair
  // lookups, so the two stay aligned. Display-only.
  const IST_OFFSET_SECONDS = 5.5 * 3600;
  function toUnix(ts) { return Math.floor(new Date(ts).getTime() / 1000) + IST_OFFSET_SECONDS; }

  function buildLegend(container, series, data) {
    container.innerHTML = '';
    const legend = document.createElement('div');
    legend.className = 'flex flex-wrap items-center gap-3 text-xs mt-2';
    series.forEach(s => {
      const wrapper = document.createElement('button');
      wrapper.className = 'flex items-center gap-1.5 px-2 py-1 rounded hover:bg-white/5 transition';
      wrapper.dataset.key = s.key;
      wrapper.innerHTML = `
        <span class="inline-block w-4 h-0.5" style="background:${s.color};${s.dash ? 'border-top:1px ' + s.dash + ' ' + s.color + ';height:0;' : ''}"></span>
        <span class="font-mono">${s.label}</span>
        <span class="text-slate-500" data-value></span>`;
      wrapper.addEventListener('click', () => {
        s.visible = !s.visible;
        s.handle.applyOptions({ visible: s.visible });
        wrapper.style.opacity = s.visible ? '1' : '0.4';
      });
      legend.appendChild(wrapper);
    });
    container.appendChild(legend);
    return legend;
  }

  function updateLegendValues(legend, series, point) {
    legend.querySelectorAll('button').forEach(btn => {
      const key = btn.dataset.key;
      const v = point ? point[key] : null;
      const span = btn.querySelector('[data-value]');
      span.textContent = v == null ? '' : '· ₹' + Math.round(v).toLocaleString('en-IN');
    });
  }

  /**
   * data: [{ts, equity, nifty, sensex}]
   * el:   target div
   */
  function renderEquityChart(el, data) {
    el.innerHTML = '';
    if (!data || !data.length) {
      el.innerHTML = '<div class="text-slate-500 text-sm h-full flex items-center justify-center">No equity history yet.</div>';
      return null;
    }

    const colors = readThemeColors();
    // Ensure the container is a flex column so the chart can grow and the
    // legend stays pinned below it without overflowing into the next card.
    el.style.display = 'flex';
    el.style.flexDirection = 'column';

    const chartHost = document.createElement('div');
    chartHost.style.flex = '1 1 auto';
    chartHost.style.minHeight = '0';   // critical: lets flex shrink the chart instead of overflowing
    chartHost.style.width = '100%';
    el.appendChild(chartHost);

    const chart = LightweightCharts.createChart(chartHost, {
      layout: { background: { color: 'transparent' }, textColor: colors.muted },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      timeScale: { timeVisible: false, secondsVisible: false, borderColor: colors.grid },
      rightPriceScale: { borderColor: colors.grid },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });

    const series = [
      { key: 'equity', label: 'Strategy', color: colors.accent, lineWidth: 2,        lineStyle: 0, dash: '' },
      { key: 'nifty',  label: 'NIFTY 50', color: colors.muted,  lineWidth: 1.5,      lineStyle: 2, dash: 'dotted' },
      { key: 'sensex', label: 'SENSEX',   color: colors.muted,  lineWidth: 1.5,      lineStyle: 3, dash: 'dashed' },
    ];

    // Compute the spread across every series so we can short-circuit the chart's
    // ultra-tight autoscale on flat / single-point data (e.g. day 1, only the
    // seed equity row exists and benchmarks are still null).
    const allValues = [];
    for (const s of series) {
      for (const d of data) if (d[s.key] != null) allValues.push(d[s.key]);
    }
    const vmin = allValues.length ? Math.min(...allValues) : 0;
    const vmax = allValues.length ? Math.max(...allValues) : 0;
    const flat = (vmax - vmin) / (vmax || 1) < 0.001;   // < 0.1% spread → "flat"

    for (const s of series) {
      s.handle = chart.addLineSeries({
        color: s.color,
        lineWidth: s.lineWidth,
        lineStyle: s.lineStyle,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      const points = data
        .filter(d => d[s.key] != null)
        .map(d => ({ time: toUnix(d.ts), value: d[s.key] }));
      s.handle.setData(points);
      s.visible = true;
    }

    // When the data is effectively flat, pin the price scale to a humane window
    // around the value so the Y-axis doesn't show 49999.94 → 50000.06.
    if (flat && allValues.length) {
      const mid = vmax;
      const pad = Math.max(mid * 0.05, 100);   // ±5% or ±₹100, whichever is larger
      for (const s of series) {
        s.handle.applyOptions({
          autoscaleInfoProvider: () => ({
            priceRange: { minValue: mid - pad, maxValue: mid + pad },
          }),
        });
      }
    }

    chart.timeScale().fitContent();

    // Resize observer keeps the chart sized to its container on layout changes.
    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: chartHost.clientWidth, height: chartHost.clientHeight });
    });
    ro.observe(chartHost);

    // Legend below the chart with click-to-toggle visibility.
    const legendHost = document.createElement('div');
    legendHost.style.flex = '0 0 auto';
    el.appendChild(legendHost);
    const legend = buildLegend(legendHost, series, data);

    // Crosshair → legend value updates
    chart.subscribeCrosshairMove(param => {
      if (!param || !param.time) {
        updateLegendValues(legend, series, data[data.length - 1]);
        return;
      }
      const t = param.time;
      const point = data.find(d => toUnix(d.ts) === t);
      updateLegendValues(legend, series, point || data[data.length - 1]);
    });
    updateLegendValues(legend, series, data[data.length - 1]);

    return { chart, series, ro };
  }

  /**
   * Loads the equity payload, renders the chart, and re-renders on themechange.
   * Returns a teardown function the caller can use if desired.
   */
  async function mountEquityChart(el, portfolioId) {
    const r = await fetch(`/api/portfolio/${portfolioId}/equity`);
    if (!r.ok) {
      el.innerHTML = `<div class="text-down text-sm">Failed to load equity (HTTP ${r.status}).</div>`;
      return () => {};
    }
    const data = await r.json();

    let current = renderEquityChart(el, data);
    const onTheme = () => {
      if (current && current.chart) {
        try { current.chart.remove(); } catch (e) {}
        if (current.ro) try { current.ro.disconnect(); } catch (e) {}
      }
      current = renderEquityChart(el, data);
    };
    window.addEventListener('themechange', onTheme);

    return () => {
      window.removeEventListener('themechange', onTheme);
      if (current && current.chart) {
        try { current.chart.remove(); } catch (e) {}
        if (current.ro) try { current.ro.disconnect(); } catch (e) {}
      }
    };
  }

  // Expose a tiny global API.
  window.PaperTradingCharts = { mountEquityChart, renderEquityChart };
})();
