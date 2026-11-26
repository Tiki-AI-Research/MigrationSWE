"""Select the executable built by a multi-command action without Go/Docker."""
from pathlib import Path
import tempfile
import unittest

from launch import inspect_action
from public_behavior import action_main


class MainSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "go.mod").write_text("module example.test/action\n")
        (self.root / "main.go").write_text("package main\nfunc main() {}\n")
        (self.root / "cmd/release-action").mkdir(parents=True)
        (self.root / "cmd/release-action/main.go").write_text("package main\nfunc main() {}\n")
        (self.root / "action.yml").write_text("runs:\n  using: docker\n  image: Dockerfile\n")
        self.mains = [{"Dir": str(self.root), "ImportPath": "example.test/action"},
                      {"Dir": str(self.root / "cmd/release-action"),
                       "ImportPath": "example.test/action/cmd/release-action"}]

    def tearDown(self):
        self.temp.cleanup()

    def docker(self, build, copy="COPY . .", entry="/usr/local/bin/action"):
        (self.root / "Dockerfile").write_text(
            "FROM golang:1.23 AS builder\nWORKDIR /src\n" + copy + "\nRUN " + build +
            '\nFROM alpine:3.20\nCOPY --from=builder /out/action /usr/local/bin/action\n'
            'ENTRYPOINT ["' + entry + '"]\n')

    def test_root_main_with_second_unused_command(self):
        for copy in ["COPY . .", "COPY go.mod ./\nCOPY main.go ./\nCOPY cmd ./cmd"]:
            with self.subTest(copy=copy):
                self.docker("CGO_ENABLED=0 go build -trimpath -ldflags=\"-s -w\" -o /out/action .", copy)
                self.assertEqual(action_main(self.root, self.mains), self.mains[0])

    def test_selected_subdirectory_and_global_c_flag(self):
        for command in ["go build -o /out/action ./cmd/release-action",
                        "go -C /src/cmd/release-action build -o /out/action ."]:
            with self.subTest(command=command):
                self.docker(command)
                self.assertEqual(action_main(self.root, self.mains), self.mains[1])

    def test_unused_build_and_node_entry_do_not_select_main(self):
        for entry in ["/not-the-built-action", "node"]:
            with self.subTest(entry=entry):
                self.docker("go build -o /out/action .", entry=entry)
                with self.assertRaisesRegex(AssertionError, "Cannot identify"):
                    action_main(self.root, self.mains)

    def test_composite_go_run_selects_main(self):
        (self.root / "action.yml").write_text(
            "runs:\n  using: composite\n  steps:\n    - run: go run ./cmd/release-action\n      shell: bash\n")
        self.assertEqual(action_main(self.root, self.mains), self.mains[1])

    def test_empty_and_ambiguous_package_lists_are_rejected(self):
        self.docker("go build -o /out/action ./...")
        self.assertEqual(inspect_action(self.root, (self.root / "action.yml").read_text())["go_packages"], [])
        with self.assertRaisesRegex(AssertionError, "Cannot identify"):
            action_main(self.root, self.mains)
        with self.assertRaisesRegex(AssertionError, "Cannot identify"):
            action_main(self.root, [])


if __name__ == "__main__":
    unittest.main()
