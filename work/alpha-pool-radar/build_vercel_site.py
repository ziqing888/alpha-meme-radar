#!/usr/bin/env python3
from __future__ import annotations

import site_builder as _site_builder


DEFAULT_OUT_DIR = _site_builder.DEFAULT_OUT_DIR
DEFAULT_REPORT = _site_builder.DEFAULT_REPORT
parse_args = _site_builder.parse_args
main = _site_builder.main
run_frontend_build = _site_builder.run_frontend_build


def build_site(report_path, out_dir):
    original = _site_builder.run_frontend_build
    _site_builder.run_frontend_build = run_frontend_build
    try:
        return _site_builder.build_site(report_path, out_dir)
    finally:
        _site_builder.run_frontend_build = original


__all__ = ["DEFAULT_OUT_DIR", "DEFAULT_REPORT", "build_site", "main", "parse_args", "run_frontend_build"]


if __name__ == "__main__":
    raise SystemExit(main())
