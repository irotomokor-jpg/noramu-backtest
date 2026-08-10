#!/usr/bin/env python3
"""Runner for v0.30 while keeping the frozen v0.29 control's max_hold contract."""
import argparse

# v0.29's frozen simulator reads args.max_hold. v0.30 tests hold horizons per
# variant, so the only consumer of this compatibility default is the same-run
# v0.29 control path.
argparse.Namespace.max_hold = 26

import kr_level_rr_v030_regime_robustness as v30

if __name__ == "__main__":
    v30.main()
