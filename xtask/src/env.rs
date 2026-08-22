use std::{collections::BTreeMap, ffi::OsString, path::Path};

pub type Environment = BTreeMap<OsString, OsString>;
pub const CREDENTIALS: &[&str] = &[
    "CARGO_REGISTRY_TOKEN",
    "CARGO_REGISTRIES_CRATES_IO_TOKEN",
    "PYPI_TOKEN",
    "TWINE_USERNAME",
    "TWINE_PASSWORD",
    "TWINE_REPOSITORY_URL",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
];

fn credential(key: &std::ffi::OsStr) -> bool {
    let key = key.to_string_lossy().to_ascii_uppercase();
    CREDENTIALS.contains(&key.as_str())
        || (key.starts_with("CARGO_REGISTRIES_") && key.ends_with("_TOKEN"))
}

pub fn sanitize(mut environment: Environment) -> Environment {
    environment.retain(|key, _| !credential(key));
    environment
}

pub fn clean() -> Environment {
    // Filter by key before retaining any values. Never inspect credential values.
    let mut result: Environment = std::env::vars_os()
        .filter(|(key, _)| !credential(key))
        .collect();
    result.remove(&OsString::from("PYTHONPATH"));
    for (key, value) in [
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_INPUT", "1"),
    ] {
        result.insert(key.into(), value.into());
    }
    result
}

pub fn python(root: &Path) -> Environment {
    let mut result = clean();
    result.insert(
        "PYTHONPATH".into(),
        std::env::join_paths([
            root.join("src"),
            root.join("packages/airpods-client-python/src"),
        ])
        .expect("repository paths contain no PATH separator"),
    );
    result
}

pub fn build(python: &Path) -> anyhow::Result<Environment> {
    let mut result = clean();
    let old = result
        .get(&OsString::from("PATH"))
        .cloned()
        .unwrap_or_default();
    let mut paths = vec![
        python
            .parent()
            .expect("venv interpreter parent")
            .to_path_buf(),
    ];
    paths.extend(std::env::split_paths(&old));
    result.insert("PATH".into(), std::env::join_paths(paths)?);
    Ok(result)
}
