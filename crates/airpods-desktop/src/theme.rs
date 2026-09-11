use eframe::egui::{self, Align2, Color32, FontId, Painter, Pos2, Rect, Stroke, StrokeKind};

pub const BACKGROUND: Color32 = Color32::from_rgb(12, 17, 23);
pub const SURFACE: Color32 = Color32::from_rgb(18, 25, 34);
pub const ELEVATED: Color32 = Color32::from_rgb(22, 31, 41);
pub const BORDER: Color32 = Color32::from_rgb(33, 44, 56);
pub const TEXT: Color32 = Color32::from_rgb(232, 239, 245);
pub const SECONDARY: Color32 = Color32::from_rgb(159, 176, 190);
pub const MUTED: Color32 = Color32::from_rgb(119, 140, 157);
pub const TEAL: Color32 = Color32::from_rgb(112, 216, 196);
pub const PINK: Color32 = Color32::from_rgb(230, 140, 163);
pub const AMBER: Color32 = Color32::from_rgb(224, 184, 115);
pub const ERROR: Color32 = Color32::from_rgb(221, 139, 137);

// One scale for the dashboard, cards, labels, and compact controls.
pub const SMALL_GAP: f32 = 8.0;
pub const GAP: f32 = 16.0;
pub const PAD: f32 = 24.0;
pub const RADIUS: u8 = 12;
pub const CONTROL_HEIGHT: f32 = 32.0;
pub const CAPTION: f32 = 11.0;
pub const LABEL: f32 = 12.0;
pub const BODY: f32 = 13.0;
pub const TITLE: f32 = 16.0;
pub const HEADER_HEIGHT: f32 = 64.0;
pub const HERO_HEIGHT: f32 = 296.0;
pub const MIN_HERO_HEIGHT: f32 = 240.0;
pub const STAT_HEIGHT: f32 = 92.0;
pub const DETAIL_HEIGHT: f32 = 218.0;
pub const FOOTER_HEIGHT: f32 = 18.0;
pub const TIMELINE_ROW: f32 = 26.0;
pub const PULSE_DURATION: std::time::Duration = std::time::Duration::from_millis(200);

pub fn install(ctx: &egui::Context) {
    let mut style = egui::Style {
        visuals: egui::Visuals::dark(),
        ..Default::default()
    };
    style.visuals.panel_fill = BACKGROUND;
    style.visuals.window_fill = SURFACE;
    style.visuals.override_text_color = Some(TEXT);
    style.visuals.weak_text_color = Some(SECONDARY);
    style.visuals.selection.bg_fill = TEAL.gamma_multiply(0.2);
    style.visuals.selection.stroke = Stroke::new(1.0, TEAL);
    style.visuals.widgets.inactive.bg_fill = SURFACE;
    style.visuals.widgets.inactive.bg_stroke = Stroke::new(1.0, BORDER);
    style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(29, 45, 56);
    style.visuals.widgets.hovered.bg_stroke = Stroke::new(1.0, TEAL.gamma_multiply(0.5));
    style.visuals.widgets.active.bg_fill = Color32::from_rgb(31, 61, 64);
    for widget in [
        &mut style.visuals.widgets.inactive,
        &mut style.visuals.widgets.hovered,
        &mut style.visuals.widgets.active,
    ] {
        widget.corner_radius = 8.into();
    }
    style.spacing.item_spacing = egui::vec2(GAP, SMALL_GAP);
    style.spacing.scroll = egui::style::ScrollStyle::solid();
    style.spacing.button_padding = egui::vec2(12.0, 7.0);
    style
        .text_styles
        .insert(egui::TextStyle::Body, FontId::proportional(14.0));
    style
        .text_styles
        .insert(egui::TextStyle::Button, FontId::proportional(13.0));
    style
        .text_styles
        .insert(egui::TextStyle::Small, FontId::proportional(12.0));
    ctx.set_theme(egui::ThemePreference::Dark);
    ctx.set_style_of(egui::Theme::Dark, style);
}

pub fn card(painter: &Painter, rect: Rect) {
    surface(painter, rect, SURFACE);
}

pub fn surface(painter: &Painter, rect: Rect, fill: Color32) {
    painter.add(
        egui::epaint::Shadow {
            offset: [0, 2],
            blur: 8,
            spread: 0,
            color: Color32::from_black_alpha(24),
        }
        .as_shape(rect, RADIUS),
    );
    painter.rect(
        rect,
        RADIUS,
        fill,
        Stroke::new(1.0, BORDER),
        StrokeKind::Inside,
    );
}

pub fn text(
    painter: &Painter,
    at: Pos2,
    align: Align2,
    value: impl ToString,
    size: f32,
    color: Color32,
) -> Rect {
    painter.text(
        at,
        align,
        value.to_string(),
        FontId::proportional(size),
        color,
    )
}

pub fn mono(painter: &Painter, at: Pos2, value: impl ToString, size: f32, color: Color32) {
    painter.text(
        at,
        Align2::LEFT_CENTER,
        value.to_string(),
        FontId::monospace(size),
        color,
    );
}

pub fn badge(painter: &Painter, rect: Rect, label: &str, color: Color32, dot: bool) {
    painter.rect(
        rect,
        16,
        color.gamma_multiply(0.08),
        Stroke::new(1.0, color.gamma_multiply(0.15)),
        StrokeKind::Inside,
    );
    if dot {
        painter.circle_filled(egui::pos2(rect.left() + 16.0, rect.center().y), 3.0, color);
    }
    text(
        painter,
        rect.center() + egui::vec2(if dot { 5.0 } else { 0.0 }, 0.0),
        Align2::CENTER_CENTER,
        label,
        LABEL,
        color,
    );
}

pub fn ellipsis(painter: &Painter, rect: Rect, value: &str, size: f32, color: Color32) {
    let mut job = egui::text::LayoutJob::simple(
        value.into(),
        FontId::proportional(size),
        color,
        rect.width(),
    );
    job.wrap.max_rows = 1;
    let galley = painter.layout_job(job);
    let at = egui::pos2(rect.left(), rect.center().y - galley.size().y / 2.0);
    painter.with_clip_rect(rect).galley(at, galley, color);
}

/// A brief receipt highlight, independent of BPM and measured beat timing.
pub fn sample_pulse(age: std::time::Duration) -> f32 {
    if age >= PULSE_DURATION {
        0.0
    } else {
        (age.as_secs_f32() / PULSE_DURATION.as_secs_f32() * std::f32::consts::PI).sin()
    }
}

pub fn spinner(painter: &Painter, center: Pos2, now: std::time::Duration, color: Color32) {
    let phase = now.as_secs_f32() * 4.0;
    let points = (0..=24)
        .map(|index| {
            let angle = phase + index as f32 / 24.0 * std::f32::consts::PI * 1.4;
            center + egui::vec2(angle.cos(), angle.sin()) * 7.0
        })
        .collect();
    painter.circle_stroke(center, 7.0, Stroke::new(1.5, color.gamma_multiply(0.14)));
    painter.line(points, Stroke::new(1.5, color.gamma_multiply(0.7)));
}

/// Decorative heart; it does not represent measured beat timing.
pub fn heart(painter: &Painter, center: Pos2, size: f32, color: Color32) {
    let mut mesh = egui::Mesh::default();
    mesh.colored_vertex(center, color);
    for index in 0..64 {
        let t = index as f32 / 64.0 * std::f32::consts::TAU;
        let x = 16.0 * t.sin().powi(3);
        let y = 13.0 * t.cos() - 5.0 * (2.0 * t).cos() - 2.0 * (3.0 * t).cos() - (4.0 * t).cos();
        mesh.colored_vertex(center + egui::vec2(x, -y) * (size / 32.0), color);
        mesh.add_triangle(0, index + 1, (index + 1) % 64 + 1);
    }
    painter.add(egui::Shape::mesh(mesh));
}

pub fn brand(painter: &Painter, rect: Rect) {
    painter.rect_filled(rect, 10, TEAL.gamma_multiply(0.09));
    let c = rect.center();
    let points = [
        (-12.0, 0.0),
        (-7.0, 0.0),
        (-3.0, -8.0),
        (1.0, 8.0),
        (5.0, -4.0),
        (8.0, 0.0),
        (12.0, 0.0),
    ]
    .into_iter()
    .map(|(x, y)| c + egui::vec2(x, y))
    .collect();
    painter.line(points, Stroke::new(2.0, TEAL));
}
