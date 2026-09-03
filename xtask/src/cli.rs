use clap::{Args, Parser, Subcommand, ValueEnum};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    about = "Private release engineering tools; Python release validation remains authoritative"
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}
#[derive(Subcommand)]
pub enum Command {
    /// Hardware-independent local release checks
    Release {
        #[command(subcommand)]
        command: Release,
    },
}
#[derive(Subcommand)]
pub enum Release {
    /// Run the independent Rust shadow validator (does not publish)
    Validate(Validate),
    /// Compare independent Python oracle and Rust shadow outputs locally
    Parity(Parity),
}
#[derive(Args)]
pub struct Validate {
    #[arg(long, value_enum, default_value = "all")]
    pub scope: Scope,
    /// Outside-repository absent/empty output (required for all/artifacts)
    #[arg(long)]
    pub output_dir: Option<PathBuf>,
    /// Explicit release interpreter; otherwise AIRPODS_RELEASE_PYTHON, .venv, python3.14
    #[arg(long)]
    pub python: Option<PathBuf>,
}
#[derive(Args)]
pub struct Parity {
    /// Outside-repository absent/empty comparison directory, not a release bundle
    #[arg(long)]
    pub output_dir: PathBuf,
    #[arg(long)]
    pub python: Option<PathBuf>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Scope {
    All,
    Static,
    Python,
    Rust,
    CrossLanguage,
    Artifacts,
}
impl Scope {
    pub fn name(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::Static => "static",
            Self::Python => "python",
            Self::Rust => "rust",
            Self::CrossLanguage => "cross-language",
            Self::Artifacts => "artifacts",
        }
    }
}
