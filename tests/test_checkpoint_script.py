import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "commit-state.sh")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class CommitStateScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.remote = os.path.join(self.tmp.name, "remote.git")
        self.work = os.path.join(self.tmp.name, "work")
        git(self.tmp.name, "init", "-q", "--bare", "-b", "main", self.remote)
        git(self.tmp.name, "clone", "-q", self.remote, self.work)
        git(self.work, "checkout", "-q", "-b", "main")
        os.makedirs(os.path.join(self.work, "data"))
        with open(os.path.join(self.work, "data", "state.json"), "w") as f:
            f.write("{}\n")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
        git(self.work, "push", "-q", "-u", "origin", "main")

    def run_script(self):
        subprocess.run(["bash", SCRIPT], cwd=self.work, check=True, capture_output=True)

    def count(self):
        return int(git(self.remote, "rev-list", "--count", "main"))

    def test_commits_and_pushes_changes(self):
        with open(os.path.join(self.work, "data", "state.json"), "w") as f:
            f.write('{"a": 1}\n')
        self.run_script()
        self.assertEqual(self.count(), 2)
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s|%ae", "main"),
                         "chore: 구독 상태 갱신|41898282+github-actions[bot]@users.noreply.github.com")

    def test_noop_without_changes_and_ignores_other_dirs(self):
        self.run_script()
        with open(os.path.join(self.work, "other.txt"), "w") as f:
            f.write("x")
        self.run_script()
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
