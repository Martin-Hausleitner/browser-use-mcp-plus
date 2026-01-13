from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass
from typing import Callable

from tests._harness import Harness, start_harness


@dataclass(frozen=True)
class TestCase:
	name: str
	fn: Callable[[Harness], None]


def _load_tests() -> list[TestCase]:
	import tests as tests_pkg

	mod_names = sorted(
		f"{tests_pkg.__name__}.{m.name}" for m in pkgutil.iter_modules(tests_pkg.__path__) if m.name.startswith("test_")
	)
	cases: list[TestCase] = []
	for mod_name in mod_names:
		mod = importlib.import_module(mod_name)
		for attr_name in dir(mod):
			if not attr_name.startswith("test_"):
				continue
			fn = getattr(mod, attr_name)
			if callable(fn):
				cases.append(TestCase(name=f"{mod_name}.{attr_name}", fn=fn))
	return cases


def main() -> int:
	cases = _load_tests()
	failures: list[str] = []

	with start_harness() as h:
		for tc in cases:
			try:
				tc.fn(h)
				print(f"PASS {tc.name}")
			except Exception as exc:  # noqa: BLE001
				failures.append(f"{tc.name}: {type(exc).__name__}: {exc}")
				print(f"FAIL {tc.name}: {type(exc).__name__}: {exc}", file=sys.stderr)

	if failures:
		print("\nFailures:", file=sys.stderr)
		for f in failures:
			print(f"- {f}", file=sys.stderr)
		return 1

	print(f"\nOK ({len(cases)} tests)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
