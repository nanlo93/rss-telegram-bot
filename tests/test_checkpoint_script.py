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

    def run_script(self, check=True):
        env = {**os.environ, "COMMIT_STATE_RETRY_SLEEP": "0"}
        return subprocess.run(["bash", SCRIPT], cwd=self.work, check=check, capture_output=True, env=env)

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


    def test_unpushed_commit_is_pushed_on_next_call(self):
        """푸시가 실패해 커밋만 남았더라도, 다음 호출(변경 없음)에서 반드시 푸시되어야 한다."""
        with open(os.path.join(self.work, "data", "state.json"), "w") as f:
            f.write('{"a": 1}\n')
        good_url = git(self.work, "remote", "get-url", "origin")
        git(self.work, "remote", "set-url", "origin", os.path.join(self.tmp.name, "missing.git"))
        failed = self.run_script(check=False)
        self.assertNotEqual(failed.returncode, 0)  # 실패를 숨기지 않음
        self.assertEqual(self.count(), 1)
        self.assertEqual(git(self.work, "status", "--porcelain", "--", "data"), "")  # 커밋은 로컬에 있음

        git(self.work, "remote", "set-url", "origin", good_url)
        self.run_script()  # 작업 트리는 깨끗하지만 미푸시 커밋이 있음
        self.assertEqual(self.count(), 2)

    def test_rebases_when_remote_advanced(self):
        other = os.path.join(self.tmp.name, "other")
        git(self.tmp.name, "clone", "-q", self.remote, other)
        with open(os.path.join(other, "README.md"), "w") as f:
            f.write("x")
        git(other, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
        git(other, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "other")
        git(other, "push", "-q", "origin", "main")

        with open(os.path.join(self.work, "data", "state.json"), "w") as f:
            f.write('{"b": 2}\n')
        self.run_script()
        self.assertEqual(self.count(), 3)


if __name__ == "__main__":
    unittest.main()
