//! Custom drawing keeps the chart's geometry and the dashboard theme consistent.
//! Lines connect exact adjacent receipts; outages split the line into segments.

use crate::model::{Freshness, Model, SamplePoint, clock_time, side_label};
use crate::theme::{self, AMBER, BORDER, MUTED, PAD, SECONDARY, TEAL, TEXT};
use eframe::egui::{self, Align2, Color32, Rect, Sense, Stroke, StrokeKind};
use std::time::Duration;

pub fn show(ui: &mut egui::Ui, rect: Rect, model: &Model, now: Duration, window: &mut u64) {
    let painter = ui.painter();
    theme::card(painter, rect);
    theme::text(
        painter,
        rect.min + egui::vec2(PAD, 28.0),
        Align2::LEFT_CENTER,
        "Heart rate history",
        theme::TITLE,
        TEXT,
    );
    let controls = Rect::from_min_size(
        egui::pos2(rect.right() - PAD - 144.0, rect.top() + 12.0),
        egui::vec2(144.0, theme::CONTROL_HEIGHT),
    );
    painter.rect_filled(controls, 8, theme::BACKGROUND);
    for (index, seconds) in [30, 60, 120].into_iter().enumerate() {
        let button = Rect::from_min_size(
            controls.min + egui::vec2(3.0 + index as f32 * 46.0, 3.0),
            egui::vec2(46.0, 26.0),
        );
        let selected = *window == seconds;
        let response = ui.place(
            button,
            egui::Button::new(
                egui::RichText::new(format!("{seconds}s"))
                    .size(theme::CAPTION)
                    .color(if selected { TEAL } else { SECONDARY }),
            )
            .wrap_mode(egui::TextWrapMode::Extend)
            .fill(if selected {
                theme::ELEVATED
            } else {
                Color32::TRANSPARENT
            })
            .stroke(Stroke::NONE)
            .corner_radius(6),
        );
        if response.clicked() {
            *window = seconds;
        }
        if response.hovered() {
            ui.painter()
                .rect_stroke(button, 6, Stroke::new(1.0, BORDER), StrokeKind::Inside);
        }
    }
    let painter = ui.painter();
    let plot = Rect::from_min_max(
        rect.min + egui::vec2(52.0, 72.0),
        rect.max - egui::vec2(PAD, 48.0),
    );
    let end = now.as_secs_f64().max(15.0);
    let start = (end - *window as f64).max(0.0);
    let visible: Vec<_> = model
        .history
        .iter()
        .filter(|point| point.at.as_secs_f64() >= start && point.at.as_secs_f64() <= end)
        .collect();
    let (low, high, step) = range(&visible);
    let position = |point: &SamplePoint| {
        egui::pos2(
            plot.left() + ((point.at.as_secs_f64() - start) / (end - start)) as f32 * plot.width(),
            plot.bottom() - (f32::from(point.sample.bpm) - low) / (high - low) * plot.height(),
        )
    };
    theme::text(
        painter,
        egui::pos2(plot.left() - 12.0, plot.top() - 15.0),
        Align2::RIGHT_CENTER,
        "BPM",
        theme::CAPTION,
        MUTED,
    );
    let divisions = ((high - low) / step).round() as usize;
    let stride = divisions.div_ceil(4);
    for index in 0..=divisions {
        if index % stride != 0 && index != divisions {
            continue;
        }
        let fraction = index as f32 / divisions as f32;
        let y = plot.bottom() - plot.height() * fraction;
        painter.line_segment(
            [egui::pos2(plot.left(), y), egui::pos2(plot.right(), y)],
            Stroke::new(1.0, BORDER.gamma_multiply(0.48)),
        );
        theme::text(
            painter,
            egui::pos2(plot.left() - 12.0, y),
            Align2::RIGHT_CENTER,
            format!("{:.0}", low + (high - low) * fraction),
            theme::CAPTION,
            MUTED,
        );
    }
    for index in 0..=4 {
        let fraction = index as f32 / 4.0;
        let x = plot.left() + plot.width() * fraction;
        painter.line_segment(
            [egui::pos2(x, plot.top()), egui::pos2(x, plot.bottom())],
            Stroke::new(1.0, BORDER.gamma_multiply(0.23)),
        );
        theme::text(
            painter,
            egui::pos2(x, plot.bottom() + 17.0),
            Align2::CENTER_CENTER,
            clock_time(Duration::from_secs_f64(
                start + (end - start) * f64::from(fraction),
            )),
            theme::CAPTION,
            MUTED,
        );
    }
    let clipped = painter.with_clip_rect(plot.expand(2.0));
    let mut line = Vec::new();
    for (index, point) in visible.iter().enumerate() {
        if index > 0 && point.segment != visible[index - 1].segment {
            draw_segment(&clipped, std::mem::take(&mut line), plot.bottom());
            gap(
                &clipped,
                plot,
                position(visible[index - 1]).x,
                position(point).x,
            );
        }
        line.push(position(point));
    }
    draw_segment(&clipped, line, plot.bottom());
    if let Some(last) = visible.last() {
        if model.freshness(now) == Freshness::Fresh {
            let at = position(last);
            let pulse = theme::sample_pulse(now.saturating_sub(last.at));
            clipped.circle_filled(
                at,
                7.0 + 2.0 * pulse,
                TEAL.gamma_multiply(0.08 + 0.03 * pulse),
            );
            clipped.circle_filled(at, 3.5, TEAL);
            clipped.circle_stroke(at, 3.5, Stroke::new(1.5, theme::SURFACE));
        } else {
            let until =
                plot.left() + ((now.as_secs_f64() - start) / (end - start)) as f32 * plot.width();
            gap(&clipped, plot, position(last).x, until);
        }
    }
    if visible.is_empty() {
        theme::text(
            painter,
            plot.center() - egui::vec2(0.0, 8.0),
            Align2::CENTER_CENTER,
            "Waiting for heart-rate data",
            theme::TITLE,
            SECONDARY,
        );
        theme::text(
            painter,
            plot.center() + egui::vec2(0.0, 18.0),
            Align2::CENTER_CENTER,
            "Readings appear here as they arrive",
            theme::LABEL,
            MUTED,
        );
    }
    theme::text(
        painter,
        egui::pos2(rect.left() + PAD, rect.bottom() - 16.0),
        Align2::LEFT_CENTER,
        "Session time",
        theme::CAPTION,
        MUTED,
    );
    theme::text(
        painter,
        egui::pos2(rect.right() - PAD, rect.bottom() - 16.0),
        Align2::RIGHT_CENTER,
        "Exact samples",
        theme::CAPTION,
        MUTED,
    );

    let response = ui.interact(plot, ui.id().with("heart-rate-plot"), Sense::hover());
    if let Some(pointer) = response.hover_pos()
        && let Some(nearest) = visible.iter().min_by(|a, b| {
            position(a)
                .distance_sq(pointer)
                .total_cmp(&position(b).distance_sq(pointer))
        })
    {
        let at = position(nearest);
        clipped.line_segment(
            [
                egui::pos2(at.x, plot.top()),
                egui::pos2(at.x, plot.bottom()),
            ],
            Stroke::new(1.0, SECONDARY.gamma_multiply(0.4)),
        );
        clipped.circle_filled(at, 4.0, TEAL);
        response.on_hover_text(format!(
            "{} BPM · {}\nReceived at {}",
            nearest.sample.bpm,
            side_label(nearest.sample.source_side),
            clock_time(nearest.at)
        ));
    }
}

fn range(points: &[&SamplePoint]) -> (f32, f32, f32) {
    let Some(min) = points.iter().map(|point| point.sample.bpm).min() else {
        return (80.0, 120.0, 10.0);
    };
    let max = points
        .iter()
        .map(|point| point.sample.bpm)
        .max()
        .unwrap_or(min);
    let step = axis_step(f32::from(max) - f32::from(min) + 16.0);
    let low = ((f32::from(min) - 8.0) / step).floor().max(0.0) * step;
    let high = ((f32::from(max) + 8.0) / step).ceil() * step;
    (low, high.max(low + step * 4.0), step)
}

fn axis_step(span: f32) -> f32 {
    if span > 120.0 {
        50.0
    } else if span > 60.0 {
        20.0
    } else {
        10.0
    }
}

fn gap(painter: &egui::Painter, plot: Rect, start: f32, end: f32) {
    if end <= start {
        return;
    }
    let rect = Rect::from_min_max(
        egui::pos2(start, plot.top()),
        egui::pos2(end, plot.bottom()),
    );
    painter.rect_filled(rect, 0, AMBER.gamma_multiply(0.025));
    painter.line_segment(
        [rect.left_top(), rect.left_bottom()],
        Stroke::new(1.0, AMBER.gamma_multiply(0.13)),
    );
    if rect.width() > 72.0 {
        let label = Rect::from_center_size(
            egui::pos2(rect.center().x, plot.top() + 16.0),
            egui::vec2(68.0, 22.0),
        );
        painter.rect_filled(label, 6, theme::BACKGROUND.gamma_multiply(0.7));
        theme::text(
            painter,
            label.center(),
            Align2::CENTER_CENTER,
            "No samples",
            theme::CAPTION,
            AMBER,
        );
    }
}

fn draw_segment(painter: &egui::Painter, points: Vec<egui::Pos2>, bottom: f32) {
    if points.len() == 1 {
        painter.circle_filled(points[0], 2.5, TEAL);
        return;
    }
    if points.is_empty() {
        return;
    }
    let mut mesh = egui::Mesh::default();
    for pair in points.windows(2) {
        let index = mesh.vertices.len() as u32;
        mesh.colored_vertex(pair[0], TEAL.gamma_multiply(0.11));
        mesh.colored_vertex(pair[1], TEAL.gamma_multiply(0.11));
        mesh.colored_vertex(egui::pos2(pair[1].x, bottom), Color32::TRANSPARENT);
        mesh.colored_vertex(egui::pos2(pair[0].x, bottom), Color32::TRANSPARENT);
        mesh.add_triangle(index, index + 1, index + 2);
        mesh.add_triangle(index, index + 2, index + 3);
    }
    painter.add(egui::Shape::mesh(mesh));
    painter.line(points, Stroke::new(2.0, TEAL));
}
