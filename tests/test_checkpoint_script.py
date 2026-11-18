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

    def run_script(self, check=True, **extra_env):
        env = {**os.environ, "COMMIT_STATE_RETRY_SLEEP": "0", **extra_env}
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


    def test_commits_only_state_dir_even_if_other_files_are_staged(self):
        with open(os.path.join(self.work, "other.txt"), "w") as f:
            f.write("unrelated")
        git(self.work, "add", "other.txt")  # 이미 스테이징된 다른 파일
        with open(os.path.join(self.work, "data", "state.json"), "w") as f:
            f.write('{"a": 1}\n')
        self.run_script()
        files = git(self.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").split()
        self.assertEqual(files, ["data/state.json"])
        self.assertIn("other.txt", git(self.work, "diff", "--cached", "--name-only"))  # 스테이징은 그대로 보존

    def test_rejects_data_dir_outside_repository(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "state.json"), "w") as f:
            f.write("{}")
        r = self.run_script(check=False, DATA_DIR=outside)
        self.assertNotEqual(r.returncode, 0)  # 성공으로 오해하게 두지 않음
        self.assertIn("저장소", r.stderr.decode())
        self.assertEqual(self.count(), 1)

    def test_rejects_gitignored_data_dir(self):
        """ignore 대상이면 status에 안 나타나 '변경 없음'으로 성공해 버리므로 실패로 처리해야 한다."""
        with open(os.path.join(self.work, ".gitignore"), "w") as f:
            f.write("custom/\n")
        os.makedirs(os.path.join(self.work, "custom", "state"))
        with open(os.path.join(self.work, "custom", "state", "state.json"), "w") as f:
            f.write("{}")
        r = self.run_script(check=False, DATA_DIR="custom/state")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gitignore", r.stderr.decode())

    def test_rejects_repository_root_as_data_dir(self):
        self.assertNotEqual(self.run_script(check=False, DATA_DIR=".").returncode, 0)

    def test_custom_data_dir_inside_repository(self):
        os.makedirs(os.path.join(self.work, "custom", "state"))
        with open(os.path.join(self.work, "custom", "state", "state.json"), "w") as f:
            f.write("{}")
        self.run_script(DATA_DIR="custom/state")
        self.assertEqual(self.count(), 2)
        self.assertEqual(git(self.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").split(), ["custom/state/state.json"])


if __name__ == "__main__":
    unittest.main()
