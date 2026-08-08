#![forbid(unsafe_code)]

use clap::Parser;

#[tokio::main(flavor = "current_thread")]
async fn main() -> std::process::ExitCode {
    match airpodsctl::run(airpodsctl::Cli::parse()).await {
        Ok(code) => std::process::ExitCode::from(code),
        Err(error) => {
            eprintln!("airpodsctl: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
