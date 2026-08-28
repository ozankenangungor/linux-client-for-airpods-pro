use crate::{
    command::{self, TEST_TIMEOUT},
    env,
    python::Python,
};
use anyhow::Result;
use std::{ffi::OsString, path::Path};
pub fn checks(root: &Path, python: &Python) -> Result<()> {
    let mut argv = vec![
        python.path.as_os_str().to_owned(),
        "-m".into(),
        "unittest".into(),
        "-v".into(),
    ];
    argv.extend(
        [
            "tests.test_rust_client_hubd_integration",
            "tests.test_airpodsctl_hubd_integration",
            "tests.test_resilient_client_hubd_integration",
            "tests.test_airpodsctl_top_integration",
            "tests.test_python_client_hubd_integration",
            "tests.test_mixed_client_hubd_integration",
            "tests.test_c_ffi_hubd_integration",
        ]
        .map(OsString::from),
    );
    command::run(argv, root, &env::python(root), TEST_TIMEOUT, false)?;
    Ok(())
}
