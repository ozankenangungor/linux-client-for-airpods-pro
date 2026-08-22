use crate :: { paths } ;

use std :: { fs } ;



// Use this test executable as the deterministic child; no shell, hardware or network.











fn path_fixture() -> (tempfile::TempDir, std::path::PathBuf) {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    fs::create_dir(&repo).unwrap();
    (temp, repo)
}
#[test]
fn output_outside_repo() {
    let (t, r) = path_fixture();
    assert!(paths::output_dir(&t.path().join("release"), &r).is_ok());
}
#[test]
fn output_repo_root_rejected() {
    let (_t, r) = path_fixture();
    assert!(paths::output_dir(&r, &r).is_err());
}
#[test]
fn output_repo_child_rejected() {
    let (_t, r) = path_fixture();
    assert!(paths::output_dir(&r.join("release"), &r).is_err());
}
#[test]
fn output_parent_normalization_rejected() {
    let (t, r) = path_fixture();
    assert!(paths::output_dir(&t.path().join("missing/../repo/release"), &r).is_err());
}
#[test]
fn output_symlink_into_repo_rejected() {
    let (t, r) = path_fixture();
    let link = t.path().join("outside");
    std::os::unix::fs::symlink(&r, &link).unwrap();
    assert!(paths::output_dir(&link.join("release"), &r).is_err());
}
#[test]
fn output_symlink_parent_resolved_before_dotdot() {
    let (t, r) = path_fixture();
    fs::create_dir(r.join("child")).unwrap();
    let link = t.path().join("outside");
    std::os::unix::fs::symlink(r.join("child"), &link).unwrap();
    assert!(paths::output_dir(&link.join("../release"), &r).is_err());
}
#[test]
fn nonempty_output_rejected() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    fs::create_dir(&out).unwrap();
    fs::write(out.join("sentinel"), b"x").unwrap();
    assert!(paths::output_dir(&out, &r).is_err());
}
#[test]
fn empty_output_accepted() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    fs::create_dir(&out).unwrap();
    assert!(paths::output_dir(&out, &r).is_ok());
}
#[test]
fn dangling_symlink_output_rejected() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    std::os::unix::fs::symlink(r.join("missing"), &out).unwrap();
    assert!(paths::output_dir(&out, &r).is_err());
}




















































