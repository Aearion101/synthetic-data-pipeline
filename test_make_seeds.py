import json, os, tempfile, unittest
import make_seeds


def row(i, lang, **kw):
    r = {"id": f"r{i}", "language": lang, "prompt": f"p{i}", "execution_status": "pending_user_approval",
         "support": {"llama4_documented": True}, "topic": "t"}
    r.update(kw)
    return r


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pack = os.path.join(self.d, "pack.jsonl")
        rows = [row(0, "en"), row(1, "en"), row(2, "en"), row(3, "ar"), row(4, "ar"),
                row(5, "xx", support={"llama4_documented": False}),
                row(6, "de", off_list_source_language=True),
                row(7, "fr", execution_status="held_x"),
                row(8, "fr"), row(9, "de")]
        with open(self.pack, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.rows = rows

    def run_(self, *a):
        return make_seeds.main(["--pack", self.pack, *a])

    def read(self, p):
        return [json.loads(l) for l in open(p, encoding="utf-8")]

    def test_refuses_without_approve(self):
        out = os.path.join(self.d, "o.jsonl")
        self.assertEqual(self.run_("--n", "2", "--out", out), 2)
        self.assertFalse(os.path.exists(out))

    def test_round_robin_skips_held_and_non_pending(self):
        out = os.path.join(self.d, "o.jsonl")
        self.assertEqual(self.run_("--n", "4", "--out", out, "--approve"), 0)
        got = self.read(out)
        self.assertEqual([r["id"] for r in got], ["r3", "r9", "r0", "r8"])  # ar, de, en, fr (sorted)
        ids = {r["id"] for r in got}
        self.assertFalse(ids & {"r5", "r6", "r7"})

    def test_whole_row_kept_status_and_n_pairs_set(self):
        out = os.path.join(self.d, "o.jsonl")
        self.run_("--n", "2", "--out", out, "--approve", "--pairs-per-seed", "3")
        for w in self.read(out):
            src = next(r for r in self.rows if r["id"] == w["id"])
            self.assertEqual(w["execution_status"], "eligible")
            self.assertEqual(w["n_pairs"], 3)
            for k, v in src.items():
                if k != "execution_status":
                    self.assertEqual(w[k], v)

    def test_source_pack_unchanged(self):
        before = open(self.pack, "rb").read()
        self.run_("--n", "2", "--out", os.path.join(self.d, "o.jsonl"), "--approve")
        self.assertEqual(open(self.pack, "rb").read(), before)

    def test_refuses_overwrite(self):
        out = os.path.join(self.d, "o.jsonl")
        open(out, "w").write("keep\n")
        self.assertEqual(self.run_("--n", "2", "--out", out, "--approve"), 2)
        self.assertEqual(open(out).read(), "keep\n")

    def test_too_many_requested_writes_nothing(self):
        out = os.path.join(self.d, "o.jsonl")
        self.assertEqual(self.run_("--n", "99", "--out", out, "--approve"), 2)
        self.assertFalse(os.path.exists(out))

    def test_deterministic(self):
        a, b = os.path.join(self.d, "a"), os.path.join(self.d, "b")
        self.run_("--n", "5", "--out", a, "--approve")
        self.run_("--n", "5", "--out", b, "--approve")
        self.assertEqual(open(a, "rb").read(), open(b, "rb").read())

    def test_bad_json_line_refused(self):
        with open(self.pack, "a") as f:
            f.write("{oops\n")
        out = os.path.join(self.d, "o.jsonl")
        self.assertEqual(self.run_("--n", "2", "--out", out, "--approve"), 2)
        self.assertFalse(os.path.exists(out))

    def test_real_hold_rule_is_the_runners(self):
        import runner_v2
        self.assertIs(make_seeds.hold_reason, runner_v2.hold_reason)


if __name__ == "__main__":
    unittest.main()
