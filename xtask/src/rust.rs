use crate::{
    command::{self, TEST_TIMEOUT},
    env,
};
use anyhow::Result;
use std::path::Path;
pub fn checks(root: &Path) -> Result<()> {
    for argv in [
        vec!["cargo", "fmt", "--check"],
        vec!["cargo", "check", "--workspace", "--locked"],
        vec!["cargo", "test", "--workspace", "--locked"],
        vec![
            "cargo",
            "clippy",
            "--workspace",
            "--all-targets",
            "--all-features",
            "--locked",
            "--",
            "-D",
            "warnings",
        ],
    ] {
        command::run(argv, root, &env::clean(), TEST_TIMEOUT, false)?;
    }
    Ok(())
}
