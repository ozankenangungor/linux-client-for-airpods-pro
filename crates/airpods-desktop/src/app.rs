use crate :: bootstrap :: Stage ;
use crate :: chart ;
use crate :: demo :: LOOP_DURATION ;
use crate :: model :: { AppEvent , Connection , Freshness , Model , Problem , TimedEvent , clock_time , side_label } ;
use crate :: source :: DataSource ;
use crate :: theme :: { self , AMBER , BACKGROUND , BORDER , ERROR , GAP , MUTED , PAD , PINK , SECONDARY , TEAL , TEXT } ;
use eframe :: egui :: { self , Align2 , Color32 , Rect , Stroke } ;
use std :: path :: PathBuf ;
use std :: time :: { Duration , Instant } ;

pub struct DesktopApp {
    model: Model,
    source: DataSource,
    started: Instant,
    now: Duration,
    chart_window: u64,
    retry_pending: bool,
}

impl DesktopApp {
    pub fn new(cc: &eframe::CreationContext<'_>, demo: bool, socket: Option<PathBuf>) -> Self {
        theme::install(&cc.egui_ctx);
        let started = Instant::now();
        let mut model = Model::default();
        let source = match DataSource::start(demo, socket, cc.egui_ctx.clone(), started) {
            Ok(source) => source,
            Err(_) => {
                model.apply(TimedEvent {
                    at: Duration::ZERO,
                    event: AppEvent::Failed(Problem::Worker),
                });
                // No fallback to simulated readings in real mode.
                return Self {
                    model,
                    source: DataSource::RealFailed,
                    started,
                    now: Duration::ZERO,
                    chart_window: 60,
                    retry_pending: false,
                };
            }
        };
        Self {
            model,
            source,
            started,
            now: Duration::ZERO,
            chart_window: 60,
            retry_pending: false,
        }
    }

    fn status(&self, now: Duration) -> (&'static str, Color32) {
        match self.model.connection {
            Connection::Preparing(Stage::CheckingDaemon) => ("Checking daemon", SECONDARY),
            Connection::Preparing(Stage::StartingDaemon | Stage::WaitingForDaemon) => {
                ("Starting daemon", SECONDARY)
            }
            Connection::Preparing(_) => ("Preparing daemon", SECONDARY),
            Connection::Idle => ("Idle", MUTED),
            Connection::Connecting => ("Connecting", SECONDARY),
            Connection::Connected => ("Ready", TEAL),
            Connection::Streaming => {
                if self.model.freshness(now) == Freshness::Fresh {
                    ("Streaming", TEAL)
                } else {
                    ("Waiting for sample", AMBER)
                }
            }
            Connection::Reconnecting { .. } => ("Reconnecting", AMBER),
            Connection::Disconnected => ("Disconnected", SECONDARY),
            Connection::Error => ("Needs attention", ERROR),
        }
    }

    fn dashboard(&mut self, ui: &mut egui::Ui, now: Duration, viewport_height: f32) {
        ui.spacing_mut().item_spacing.y = 0.0;
        let width = ui.available_width();
        let start_y = ui.cursor().top();
        let header = allocate(ui, theme::HEADER_HEIGHT);
        self.header(ui.painter(), header, now);
        ui.add_space(PAD);
        self.notice(ui, now);
        let below_chart =
            theme::STAT_HEIGHT + theme::DETAIL_HEIGHT + theme::FOOTER_HEIGHT + 3.0 * GAP;
        let used_height = ui.cursor().top() - start_y;
        let top_height = (viewport_height - used_height - below_chart)
            .clamp(theme::MIN_HERO_HEIGHT, theme::HERO_HEIGHT);
        let top = allocate(ui, top_height);
        let hero_width = (width * 0.25).clamp(238.0, 320.0);
        let hero = Rect::from_min_size(top.min, egui::vec2(hero_width, top.height()));
        self.hero(ui.painter(), hero, now);
        let chart = Rect::from_min_max(egui::pos2(hero.right() + GAP, top.top()), top.max);
        chart::show(ui, chart, &self.model, now, &mut self.chart_window);
        ui.add_space(GAP);
        let footer = allocate(ui, theme::FOOTER_HEIGHT);
        theme::text(
            ui.painter(),
            footer.left_center(),
            Align2::LEFT_CENTER,
            "Experimental data · Not for medical use",
            theme::CAPTION,
            MUTED,
        );
        let mode = if self.source.is_demo() {
            format!(
                "Showroom · simulated feed · {}s loop",
                LOOP_DURATION.as_secs()
            )
        } else {
            "Local connection · airpods-hubd".into()
        };
        theme::text(
            ui.painter(),
            footer.right_center(),
            Align2::RIGHT_CENTER,
            mode,
            theme::CAPTION,
            MUTED,
        );
    }

    fn header(&self, painter: &egui::Painter, rect: Rect, now: Duration) {
        theme::brand(
            painter,
            Rect::from_min_size(rect.min + egui::vec2(0.0, 4.0), egui::vec2(40.0, 40.0)),
        );
        theme::text(
            painter,
            rect.min + egui::vec2(52.0, 22.0),
            Align2::LEFT_CENTER,
            "AirPods HR",
            24.0,
            TEXT,
        );
        theme::text(
            painter,
            rect.min + egui::vec2(52.0, 45.0),
            Align2::LEFT_CENTER,
            "Heart-rate monitor",
            theme::LABEL,
            MUTED,
        );
        let (status, color) = self.status(now);
        let badge_width = 156.0;
        theme::badge(
            painter,
            Rect::from_min_size(
                egui::pos2(rect.right() - badge_width, rect.top() + 4.0),
                egui::vec2(badge_width, theme::CONTROL_HEIGHT),
            ),
            status,
            color,
            true,
        );
        if self.source.is_demo() {
            theme::badge(
                painter,
                Rect::from_min_size(
                    egui::pos2(rect.right() - badge_width - 80.0, rect.top() + 4.0),
                    egui::vec2(68.0, theme::CONTROL_HEIGHT),
                ),
                "DEMO",
                SECONDARY,
                false,
            );
        }
        theme::text(
            painter,
            egui::pos2(rect.right(), rect.top() + 50.0),
            Align2::RIGHT_CENTER,
            format!("Session  {}", clock_time(now)),
            theme::CAPTION,
            MUTED,
        );
        painter.line_segment(
            [rect.left_bottom(), rect.right_bottom()],
            Stroke::new(1.0, BORDER.gamma_multiply(0.65)),
        );
    }

    fn hero(&self, painter: &egui::Painter, rect: Rect, now: Duration) {
        theme::surface(painter, rect, theme::ELEVATED);
        theme::text(
            painter,
            rect.min + egui::vec2(PAD, PAD),
            Align2::LEFT_CENTER,
            "LIVE HEART RATE",
            theme::CAPTION,
            SECONDARY,
        );
        let freshness = self.model.freshness(now);
        let pulse = self.sample_pulse(now);
        let center = egui::pos2(rect.right() - PAD, rect.top() + PAD);
        painter.circle_filled(
            center,
            16.0 + 3.0 * pulse,
            PINK.gamma_multiply(0.04 + 0.04 * pulse),
        );
        theme::heart(
            painter,
            center,
            16.0 + 2.0 * pulse,
            if freshness == Freshness::Fresh {
                PINK
            } else {
                MUTED
            },
        );
        let value = if freshness != Freshness::Unavailable {
            self.model
                .latest
                .map_or_else(|| "—".into(), |point| point.sample.bpm.to_string())
        } else {
            "—".into()
        };
        let number_y = (rect.height() - 60.0) / 2.0;
        let number_size =
            (rect.height() * 0.5 - 28.0).min(if rect.width() < 280.0 { 104.0 } else { 120.0 });
        theme::text(
            painter,
            egui::pos2(rect.center().x, rect.top() + number_y),
            Align2::CENTER_CENTER,
            value,
            number_size,
            if freshness == Freshness::Fresh {
                TEXT
            } else {
                MUTED
            },
        );
        theme::text(
            painter,
            egui::pos2(
                rect.center().x,
                rect.top() + number_y + number_size / 2.0 + 2.0,
            ),
            Align2::CENTER_CENTER,
            "BPM",
            theme::BODY,
            SECONDARY,
        );
        let detail = match freshness {
            Freshness::Fresh => "Live reading",
            Freshness::Waiting => "Waiting for next reading",
            Freshness::Unavailable => self.status(now).0,
        };
        theme::text(
            painter,
            egui::pos2(
                rect.center().x,
                rect.top() + number_y + number_size / 2.0 + 20.0,
            ),
            Align2::CENTER_CENTER,
            detail,
            theme::LABEL,
            if freshness == Freshness::Fresh {
                SECONDARY
            } else {
                MUTED
            },
        );
        painter.line_segment(
            [
                egui::pos2(rect.left() + PAD, rect.bottom() - 72.0),
                egui::pos2(rect.right() - PAD, rect.bottom() - 72.0),
            ],
            Stroke::new(1.0, BORDER),
        );
        let source = self.model.latest.map_or("Awaiting first reading", |point| {
            side_label(point.sample.source_side)
        });
        theme::badge(
            painter,
            Rect::from_center_size(
                egui::pos2(rect.center().x, rect.bottom() - 48.0),
                egui::vec2(160.0, 28.0),
            ),
            source,
            if freshness == Freshness::Fresh {
                SECONDARY
            } else {
                MUTED
            },
            false,
        );
        let age = self.model.latest.map_or_else(
            || "No samples received yet".into(),
            |point| {
                let seconds = now.saturating_sub(point.at).as_secs();
                if freshness == Freshness::Fresh && seconds == 0 {
                    "Received just now".into()
                } else {
                    format!("Last reading {seconds}s ago")
                }
            },
        );
        theme::text(
            painter,
            egui::pos2(rect.center().x, rect.bottom() - 20.0),
            Align2::CENTER_CENTER,
            age,
            theme::CAPTION,
            MUTED,
        );
    }

    fn sample_pulse(&self, now: Duration) -> f32 {
        if self.model.freshness(now) == Freshness::Fresh {
            self.model.latest.map_or(0.0, |point| {
                theme::sample_pulse(now.saturating_sub(point.at))
            })
        } else {
            0.0
        }
    }

    

    

    

    fn notice(&mut self, ui: &mut egui::Ui, now: Duration) {
        let needs_notice = self.model.problem.is_some()
            || (!self.source.is_demo() && !self.model.connection.has_connection());
        if !needs_notice {
            return;
        }
        let problem = self.model.problem;
        let stage = match self.model.connection {
            Connection::Preparing(stage) => Some(stage),
            _ => None,
        };
        let hint = problem.map_or_else(
            || match self.model.connection {
                Connection::Preparing(stage) => stage.hint(),
                Connection::Reconnecting { .. } => {
                    "The local connection was interrupted. The app will retry automatically."
                }
                _ => "The first reading appears when the daemon sends a sample.",
            },
            Problem::hint,
        );
        let can_retry = self.model.connection.can_retry() && problem != Some(Problem::Worker);
        let text_left = PAD + 32.0;
        let max_width =
            ui.available_width() - text_left - PAD - if can_retry { 156.0 } else { 0.0 };
        let hint = ui.painter().layout(
            hint.into(),
            egui::FontId::proportional(theme::BODY),
            SECONDARY,
            max_width,
        );
        let rect = allocate(ui, (hint.size().y + 52.0).max(80.0));
        theme::card(ui.painter(), rect);
        let active = matches!(
            self.model.connection,
            Connection::Preparing(_) | Connection::Connecting | Connection::Reconnecting { .. }
        );
        let color = if self.model.connection == Connection::Error {
            ERROR
        } else {
            self.status(now).1
        };
        let icon = rect.min + egui::vec2(PAD + 8.0, rect.height() / 2.0);
        if active {
            theme::spinner(ui.painter(), icon, now, color);
        } else {
            ui.painter()
                .circle_filled(icon, 13.0, color.gamma_multiply(0.07));
            ui.painter().circle_filled(icon, 3.0, color);
        }
        theme::ellipsis(
            ui.painter(),
            Rect::from_min_size(
                rect.min + egui::vec2(text_left, 12.0),
                egui::vec2(max_width, 28.0),
            ),
            problem.map_or_else(
                || stage.map_or("Waiting for airpods-hubd", Stage::title),
                Problem::title,
            ),
            theme::TITLE,
            TEXT,
        );
        ui.painter()
            .galley(rect.min + egui::vec2(text_left, 42.0), hint, SECONDARY);
        if can_retry {
            let button = Rect::from_center_size(
                egui::pos2(rect.right() - PAD - 70.0, rect.center().y),
                egui::vec2(140.0, theme::CONTROL_HEIGHT),
            );
            let response = ui.place(
                button,
                egui::Button::new(
                    egui::RichText::new(if self.retry_pending {
                        "Retrying…"
                    } else {
                        "Retry connection"
                    })
                    .size(theme::LABEL)
                    .color(if self.retry_pending { SECONDARY } else { TEAL }),
                )
                .fill(TEAL.gamma_multiply(0.09))
                .stroke(Stroke::new(1.0, TEAL.gamma_multiply(0.3)))
                .corner_radius(8),
            );
            if !self.retry_pending && response.clicked() {
                self.retry_pending = self.source.retry();
            }
        }
        ui.add_space(GAP);
    }

    fn draw(&mut self, ui: &mut egui::Ui, now: Duration) {
        egui::Frame::central_panel(ui.style())
            .fill(BACKGROUND)
            .inner_margin(PAD as i8)
            .show(ui, |ui| {
                let viewport_height = ui.available_height();
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .show(ui, |ui| self.dashboard(ui, now, viewport_height));
            });
    }
}

impl eframe::App for DesktopApp {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let elapsed = self.started.elapsed();
        for event in self.source.poll(elapsed) {
            if matches!(
                event.event,
                AppEvent::Bootstrap(_) | AppEvent::Connecting | AppEvent::Failed(_)
            ) {
                self.retry_pending = false;
            }
            self.model.apply(event);
        }
        let now = self.source.time(elapsed);
        self.model.tick(now);
        // Keep every panel on the model's clock, including the demo loop edge.
        self.now = now;
        ctx.request_repaint_after(self.repaint_delay(now));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.draw(ui, self.now);
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.source.shutdown();
    }
}

impl DesktopApp {
    fn repaint_delay(&self, now: Duration) -> Duration {
        let highlighting_sample = self.model.freshness(now) == Freshness::Fresh
            && self
                .model
                .latest
                .is_some_and(|point| now.saturating_sub(point.at) < theme::PULSE_DURATION);
        if highlighting_sample {
            Duration::from_millis(16)
        } else if self.source.is_demo()
            || self.model.freshness(now) == Freshness::Fresh
            || matches!(
                self.model.connection,
                Connection::Preparing(_) | Connection::Connecting | Connection::Reconnecting { .. }
            )
        {
            Duration::from_millis(100)
        } else {
            Duration::from_secs(1)
        }
    }
}

fn allocate(ui: &mut egui::Ui, height: f32) -> Rect {
    ui.allocate_exact_size(
        egui::vec2(ui.available_width(), height),
        egui::Sense::hover(),
    )
    .0
}

#[cfg(test)]
mod tests {
    use super :: * ;
    use crate :: source :: RealSource ;

    #[test]
    fn receipt_animation_is_bounded_and_idle_repaints_stay_slow() {
        let mut app = DesktopApp {
            model: Model::default(),
            source: DataSource::RealFailed,
            started: Instant::now(),
            now: Duration::ZERO,
            chart_window: 60,
            retry_pending: false,
        };
        assert_eq!(app.repaint_delay(Duration::ZERO), Duration::from_secs(1));
        app.model.apply(TimedEvent {
            at: Duration::ZERO,
            event: AppEvent::Sample(airpods_client::HeartRateSample {
                bpm: 169,
                source_side: airpods_client::SourceSide::Right,
            }),
        });
        assert_eq!(
            app.repaint_delay(Duration::from_millis(100)),
            Duration::from_millis(16)
        );
        assert!(app.sample_pulse(Duration::from_millis(100)) > 0.99);
        assert_eq!(app.sample_pulse(Duration::from_millis(200)), 0.0);
        assert_eq!(
            app.repaint_delay(Duration::from_millis(200)),
            Duration::from_millis(100)
        );
        assert_eq!(
            app.repaint_delay(Duration::from_secs(4)),
            Duration::from_secs(1)
        );
        assert_eq!(app.model.latest.unwrap().sample.bpm, 169);
        app.model.apply(TimedEvent {
            at: Duration::from_secs(4),
            event: AppEvent::Failed(Problem::Unavailable),
        });
        assert_eq!(
            app.repaint_delay(Duration::from_secs(4)),
            Duration::from_secs(1)
        );
        app.model.apply(TimedEvent {
            at: Duration::from_secs(5),
            event: AppEvent::Bootstrap(Stage::InstallingDaemon),
        });
        assert_eq!(
            app.repaint_delay(Duration::from_secs(5)),
            Duration::from_millis(100)
        );
    }

    #[test]
    fn retry_button_accepts_mouse_input_and_queues_one_worker_command() {
        let ctx = egui::Context::default();
        theme::install(&ctx);
        let started = Instant::now();
        let path =
            std::env::temp_dir().join(format!("desktop-ui-retry-{}.sock", std::process::id()));
        assert!(!path.exists());
        let source = RealSource::start(Some(path), ctx.clone(), started).unwrap();
        let mut model = Model::default();
        model.apply(TimedEvent {
            at: Duration::ZERO,
            event: AppEvent::Failed(Problem::Unavailable),
        });
        let mut app = DesktopApp {
            model,
            source: DataSource::Real(source),
            started,
            now: Duration::ZERO,
            chart_window: 60,
            retry_pending: false,
        };
        let screen = Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(1280.0, 820.0));
        let pointer = egui::pos2(1150.0, 152.0);
        let mut frame = |time, events| {
            let input = egui::RawInput {
                screen_rect: Some(screen),
                time: Some(time),
                events,
                ..Default::default()
            };
            let mut output = ctx.run_ui(input, |ui| app.draw(ui, Duration::ZERO));
            // This interaction test has no GPU; discard the font atlas updates.
            output.textures_delta.clear();
        };
        frame(0.0, vec![]);
        frame(
            0.1,
            vec![
                egui::Event::PointerMoved(pointer),
                egui::Event::PointerButton {
                    pos: pointer,
                    button: egui::PointerButton::Primary,
                    pressed: true,
                    modifiers: egui::Modifiers::NONE,
                },
            ],
        );
        frame(
            0.2,
            vec![egui::Event::PointerButton {
                pos: pointer,
                button: egui::PointerButton::Primary,
                pressed: false,
                modifiers: egui::Modifiers::NONE,
            }],
        );
        assert!(
            app.retry_pending,
            "Mouse release must activate Retry connection"
        );
        // The bounded command slot is already occupied; rendering cannot queue
        // a duplicate retry while its first command is waiting for the worker.
        assert!(!app.source.retry());
        app.source.shutdown();
    }
}
