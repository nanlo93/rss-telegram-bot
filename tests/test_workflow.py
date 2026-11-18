import os
import unittest

WORKFLOW = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "poll.yml")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            self.text = f.read()

    def test_checkout_uses_latest_branch_tip_not_queued_sha(self):
        """concurrency로 대기하다 시작하는 실행이 앞선 실행의 최종 커밋이 빠진 SHA를 읽으면 이미 보낸 알림이 중복된다."""
        checkout = self.text.split("actions/checkout", 1)[1].split("- uses:", 1)[0]
        self.assertIn("ref: ${{ github.ref }}", checkout)

    def test_runs_are_serialized_and_final_commit_always_runs(self):
        self.assertIn("concurrency:", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("if: always()", self.text)


if __name__ == "__main__":
    unittest.main()
