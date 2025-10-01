// Weekly Checkin — Responsive dual header indicators (Confidence + Progress)
// Handles resize, avoids duplicates, works across Frappe versions.

frappe.ui.form.on('Weekly Checkin', {
    refresh(frm) {
        ensure_wc_styles();
        draw_pills(frm);
        bind_resize(frm);
    },
    confidence(frm) { draw_pills(frm); },
    progress_reported(frm) { draw_pills(frm); }
});

function draw_pills(frm) {
    // 1) Clear Frappe's default indicator pill
    frm.page.clear_indicator();

    // 2) Also remove any previously injected custom pills
    //    (we mark ours with the 'wc-pill' class)
    frm.page.$title_area.find('.indicator-pill.wc-pill').remove();

    // 3) Pill #1: Confidence
    const conf_text = frm.doc.confidence || 'No Confidence';
    frm.page.set_indicator(conf_text, confidence_color(conf_text));

    // 4) Pill #2: Progress
    const progress = clamp_0_100(frm.doc.progress_reported);
    const compact = is_compact(); // small screens → short label

    const $pill2 = $(
        `<span class="indicator-pill wc-pill ${progress_color(progress)}"
            style="margin-left:6px; white-space:nowrap; display:inline-flex; align-items:center;">
       ${compact ? `${progress}%` : `Progress ${progress}%`}
     </span>`
    );

    // Insert after the first pill Frappe rendered
    const $first = frm.page.$title_area.find('.indicator-pill').last();
    ($first.length ? $first : frm.page.$title_area).after($pill2);

    // Title area as flex so pills wrap cleanly on tiny screens
    frm.page.$title_area.css({
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: '6px'
    });
}

// --- Helpers ---

function confidence_color(v) {
    if (v === 'On Track') return 'blue';
    if (v === 'At Risk') return 'red';
    if (v === 'Blocked') return 'orange'; // closest to yellow
    return 'gray';
}

// Progress thresholds → color
// 0–29: red, 30–59: orange, 60–89: blue, 90–100: green
function progress_color(n) {
    if (n >= 90) return 'green';
    if (n >= 60) return 'blue';
    if (n >= 30) return 'orange';
    return 'red';
}

function clamp_0_100(v) {
    const n = parseInt(v, 10);
    if (isNaN(n)) return 0;
    if (n < 0) return 0;
    if (n > 100) return 100;
    return n;
}

function is_compact() {
    return window.matchMedia && window.matchMedia('(max-width: 480px)').matches;
}

function bind_resize(frm) {
    if (frm._wc_resize_bound) return;
    frm._wc_resize_bound = true;
    window.addEventListener('resize', frappe.utils.throttle(() => draw_pills(frm), 200));
}

// One-time CSS tweaks for very small screens
function ensure_wc_styles() {
    if (document.getElementById('wc-pill-styles')) return;
    const style = document.createElement('style');
    style.id = 'wc-pill-styles';
    style.textContent = `
    @media (max-width: 480px) {
      .indicator-pill.wc-pill { padding: 2px 6px; font-size: 11px; }
      .page-title .indicator-pill { margin-top: 2px; }
    }
  `;
    document.head.appendChild(style);
}
