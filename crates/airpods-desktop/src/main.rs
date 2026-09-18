#![forbid(unsafe_code)]

mod app;
mod bootstrap;
mod chart;
mod demo;
mod model;
mod source;
mod theme;

use clap::Parser;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "airpods-desktop",
    about = "Live AirPods heart rate through airpods-hubd"
)]
struct Cli {
    /// Run a repeatable showroom loop without a daemon or AirPods.
    #[arg(long, conflicts_with = "socket")]
    demo: bool,
    /// Use an externally managed daemon socket; skip automatic setup.
    #[arg(long, value_name = "PATH")]
    socket: Option<PathBuf>,
}

fn main() -> eframe::Result {
    let cli = Cli::parse();
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_title("AirPods HR")
            .with_app_id("airpods-desktop")
            .with_inner_size([1280.0, 820.0])
            .with_min_inner_size([760.0, 640.0]),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "AirPods HR",
        options,
        Box::new(move |cc| Ok(Box::new(app::DesktopApp::new(cc, cli.demo, cli.socket)))),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_keeps_demo_isolated_from_real_sockets() {
        assert!(Cli::try_parse_from(["airpods-desktop"]).is_ok());
        assert!(
            Cli::try_parse_from(["airpods-desktop", "--demo"])
                .unwrap()
                .demo
        );
        assert!(
            Cli::try_parse_from([
                "airpods-desktop",
                "--demo",
                "--socket",
                "/private/daemon.sock"
            ])
            .is_err()
        );
    }
}
