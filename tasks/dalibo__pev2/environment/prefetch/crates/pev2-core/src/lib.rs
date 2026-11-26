//! Build-time dependency prefetch stub (never shipped to the agent image).
//!
//! This crate exists only so `cargo fetch --locked` can resolve the workspace
//! in `environment/Dockerfile`; the resulting registry cache is what the agent
//! build uses offline.  It is discarded after the fetch stage.
