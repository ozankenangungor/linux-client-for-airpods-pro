#![forbid(unsafe_code)]

mod archive;
mod artifact;
mod cli;
mod command;
mod consumer;
mod cross_language;
mod env;
mod git;
mod manifest;
mod parity;
mod paths;
mod python;
mod rust;
mod static_policy;
#[cfg(test)]
mod tests;

use anyhow::{Context, Result};
use clap::Parser;
use cli::{Cli, Command, Release, Scope};
use std::path::Path;

pub const VERSION: &str = "0.1.1";

fn validate(
    root: &Path,
    python: &python::Python,
    scope: Scope,
    output: Option<&Path>,
) -> Result<()> {
    let commit = git::clean_commit(root)?;
    let output = if matches!(scope, Scope::All | Scope::Artifacts) {
        Some(paths::output_dir(
            output.context("--output-dir is required for all/artifacts validation")?,
            root,
        )?)
    } else {
        output.map(|p| paths::output_dir(p, root)).transpose()?
    };
    if matches!(scope, Scope::All | Scope::Static) {
        static_policy::validate(root)?;
    }
    if matches!(scope, Scope::All | Scope::Python) {
        python::checks(root, python)?;
    }
    if matches!(scope, Scope::All | Scope::Rust) {
        rust::checks(root)?;
    }
    if matches!(scope, Scope::All | Scope::CrossLanguage) {
        cross_language::checks(root, python)?;
    }
    if matches!(scope, Scope::All | Scope::Artifacts) {
        artifact::validate(
            root,
            python,
            output.as_deref().context("artifact output")?,
            &commit,
        )?;
    }
    git::unchanged(root, &commit)?;
    println!("xtask release validation PASS ({})", scope.name());
    Ok(())
}
fn execute() -> Result<()> {
    let cli = Cli::parse();
    let root = git::root()?;
    git::clean_commit(&root)?;
    match cli.command {
        Command::Release {
            command: Release::Validate(args),
        } => {
            let python = python::Python::select(args.python.as_deref(), &root)?;
            validate(&root, &python, args.scope, args.output_dir.as_deref())
        }
        Command::Release {
            command: Release::Parity(args),
        } => {
            let python = python::Python::select(args.python.as_deref(), &root)?;
            parity::run(&root, &python, &args.output_dir)
        }
    }
}
fn main() {
    if let Err(error) = execute() {
        eprintln!("xtask release validation failed: {error:#}");
        std::process::exit(1);
    }
}
