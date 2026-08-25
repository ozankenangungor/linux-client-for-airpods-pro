#![forbid(unsafe_code)]

mod archive;


mod command;
mod env;
mod git;
mod manifest;
mod paths;
mod python;
mod rust;
mod static_policy;
#[cfg(test)]
mod tests;






pub const VERSION: &str = "0.1.0";




