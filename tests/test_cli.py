import json
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from ebrains_bucket_sync import cli
from ebrains_bucket_sync.virtual_bucket import BucketInfo, read_bucket_info, write_bucket_info

from .conftest import FakeStorage, write

SPEC = Path(__file__).resolve().parents[1] / "spec"


def assert_matches_plan_schema(plan: dict) -> None:
    registry = {
        "sync-policy.schema.json": json.loads((SPEC / "sync-policy.schema.json").read_text())
    }
    resolver = jsonschema.RefResolver(base_uri="", referrer={}, store=registry)
    schema = json.loads((SPEC / "sync-plan.schema.json").read_text())
    jsonschema.validate(plan, schema, resolver=resolver)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_storage(monkeypatch):
    storage = FakeStorage()
    monkeypatch.setattr(cli, "make_storage", lambda: storage)
    return storage


def test_push_uploads_and_reports(runner, folder, fake_storage):
    write(folder, "a.txt", b"aaa")
    fake_storage.put("b", "extra.txt", b"e")

    result = runner.invoke(cli.main, ["push", str(folder), "b", "--prefix", "results"])

    assert result.exit_code == 0, result.output
    assert f'Syncing "{folder}" to bucket "b", folder "results/".' in result.output
    assert "1 file(s) to upload (3 B), 0 to delete, 0 unchanged." in result.output
    assert "[1/1] Upload a.txt (3 B)" in result.output
    assert "Done: 1 uploaded, 0 deleted, 0 failed." in result.output
    assert fake_storage.uploaded == [("b", "results/a.txt")]


def test_push_dry_run_lists_the_plan(runner, folder, fake_storage):
    write(folder, "a.txt", b"a")
    fake_storage.put("b", "extra.txt", b"e")

    result = runner.invoke(cli.main, ["push", str(folder), "b", "--dry-run", "--delete"])

    assert result.exit_code == 0, result.output
    assert "[dry-run] Upload a.txt (new)" in result.output
    assert "[dry-run] Delete extra.txt (extraneous)" in result.output
    assert fake_storage.uploaded == [] and fake_storage.deleted == []


def test_push_mentions_kept_objects(runner, folder, fake_storage):
    write(folder, "a.txt", b"a")
    fake_storage.put("b", "extra.txt", b"e")

    result = runner.invoke(cli.main, ["push", str(folder), "b"])

    assert (
        "1 object(s) of the bucket are not in the folder and are kept. Use --delete"
        in result.output
    )


def test_push_exits_with_2_when_refused(runner, folder, fake_storage):
    fake_storage.put("b", "extra.txt", b"e")

    result = runner.invoke(cli.main, ["push", str(folder), "b", "--delete"])

    assert result.exit_code == 2
    assert "Stopped before changing anything" in result.output
    assert fake_storage.deleted == []


def test_push_exits_with_1_when_an_upload_failed(runner, folder, fake_storage, monkeypatch):
    write(folder, "bad.txt", b"b")
    fake_storage.errors["bad.txt"] = ValueError("malformed")

    result = runner.invoke(cli.main, ["push", str(folder), "b"])

    assert result.exit_code == 1
    assert "Failed: bad.txt: malformed" in result.output
    assert "The sync is incomplete" in result.output


def test_push_writes_a_plan_file_that_matches_the_schema(runner, folder, fake_storage, tmp_path):
    write(folder, "a.txt", b"a")
    fake_storage.put("b", "extra.txt", b"e")
    plan_file = tmp_path / "plan.json"

    result = runner.invoke(
        cli.main,
        [
            "push",
            str(folder),
            "b",
            "--delete",
            "--exclude",
            "*.tmp",
            "--max-delete",
            "5",
            "--plan-file",
            str(plan_file),
            "--quiet",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == ""
    plan = json.loads(plan_file.read_text())
    assert_matches_plan_schema(plan)
    assert plan["policy"] == {
        "comparison": "SizeAndTime",
        "delete": True,
        "exclude": ["*.tmp"],
        "max_delete": 5,
    }
    assert [(a["path"], a["action"], a["status"]) for a in plan["actions"]] == [
        ("a.txt", "upload", "done"),
        ("extra.txt", "delete", "done"),
    ]


def test_push_combines_exclude_and_exclude_from(runner, folder, fake_storage, tmp_path):
    write(folder, "a.txt", b"a")
    write(folder, "b.tmp", b"b")
    write(folder, ".git/config", b"c")
    exclude_file = tmp_path / "dataset.ignore"
    exclude_file.write_text("# version control\n.git/\n")

    result = runner.invoke(
        cli.main,
        ["push", str(folder), "b", "--exclude", "*.tmp", "--exclude-from", str(exclude_file)],
    )

    assert result.exit_code == 0, result.output
    assert fake_storage.uploaded == [("b", "a.txt")]


def test_push_refuses_an_exclude_file_that_re_includes(runner, folder, fake_storage, tmp_path):
    write(folder, "a.txt", b"a")
    exclude_file = tmp_path / "dataset.ignore"
    exclude_file.write_text("*.txt\n!a.txt\n")

    result = runner.invoke(
        cli.main, ["push", str(folder), "b", "--exclude-from", str(exclude_file)]
    )

    assert result.exit_code == 2
    assert f'"{exclude_file}", line 2 ("!a.txt")' in result.output
    assert fake_storage.uploaded == []


def test_push_refuses_a_missing_exclude_file(runner, folder, fake_storage, tmp_path):
    result = runner.invoke(
        cli.main, ["push", str(folder), "b", "--exclude-from", str(tmp_path / "missing.ignore")]
    )

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert fake_storage.uploaded == []


def test_push_plan_file_of_a_refused_run_holds_the_plan(runner, folder, fake_storage, tmp_path):
    fake_storage.put("b", "extra.txt", b"e")
    plan_file = tmp_path / "plan.json"

    result = runner.invoke(
        cli.main, ["push", str(folder), "b", "--delete", "--plan-file", str(plan_file)]
    )

    assert result.exit_code == 2
    plan = json.loads(plan_file.read_text())
    assert plan["refusal"].startswith("The source has no files")
    assert plan["actions"][0]["path"] == "extra.txt"


def test_push_of_a_virtual_bucket_reports_its_placeholders(runner, folder, fake_storage, tmp_path):
    write_bucket_info(folder, BucketInfo("b"))
    write(folder, "placeholder.dat", b"")
    write(folder, "a.txt", b"a")
    fake_storage.put("b", "placeholder.dat", b"xyz")
    plan_file = tmp_path / "plan.json"

    result = runner.invoke(cli.main, ["push", str(folder), "b", "--plan-file", str(plan_file)])

    assert result.exit_code == 0, result.output
    assert "1 file(s) to upload (1 B), 0 to delete, 0 unchanged." in result.output
    assert (
        "1 empty file(s) are placeholders of objects not yet downloaded and are left alone."
        in result.output
    )
    assert fake_storage.content("b", "placeholder.dat") == b"xyz"
    plan = json.loads(plan_file.read_text())
    assert_matches_plan_schema(plan)
    assert [(a["path"], a["action"], a["reason"], a["status"]) for a in plan["actions"]] == [
        ("a.txt", "upload", "new", "done"),
        ("placeholder.dat", "none", "placeholder", ""),
    ]


def test_push_of_a_virtual_bucket_to_another_bucket_exits_with_2(
    runner, folder, fake_storage, tmp_path
):
    write_bucket_info(folder, BucketInfo("b", "results"))
    write(folder, "a.txt", b"a")
    plan_file = tmp_path / "plan.json"

    result = runner.invoke(cli.main, ["push", str(folder), "other", "--plan-file", str(plan_file)])

    assert result.exit_code == 2
    assert (
        "Stopped before changing anything: The folder is a virtual bucket of bucket 'b', "
        "folder 'results/', not of bucket 'other'." in result.output
    )
    assert fake_storage.listings == []
    plan = json.loads(plan_file.read_text())
    assert_matches_plan_schema(plan)
    assert plan["refusal"].startswith("The folder is a virtual bucket of")
    assert plan["actions"] == []


def test_push_of_a_virtual_bucket_of_a_newer_format_is_an_error(runner, folder, fake_storage):
    metadata = folder / ".ebrains"
    metadata.mkdir()
    (metadata / "bucket.json").write_text(
        json.dumps({"format_version": 2, "bucket": "b", "prefix": "", "listed_at": None})
    )

    result = runner.invoke(cli.main, ["push", str(folder), "b"])

    assert result.exit_code == 1
    assert "Error: " in result.output and "format_version 2" in result.output
    assert fake_storage.listings == []


def test_login_and_logout_use_the_authenticator(runner, monkeypatch):
    calls = []

    class Auth:
        def access_token(self, *, force_refresh=False):
            calls.append("token")
            return "t"

        def log_in_again(self):
            calls.append("log in again")

        def logout(self):
            calls.append("logout")

    monkeypatch.setattr(cli, "make_authenticator", lambda: Auth())

    assert runner.invoke(cli.main, ["login"]).exit_code == 0
    assert runner.invoke(cli.main, ["login", "--force"]).exit_code == 0
    assert runner.invoke(cli.main, ["logout"]).exit_code == 0
    assert calls == ["token", "log in again", "logout"]


def test_format_size():
    assert cli.format_size(0) == "0 B"
    assert cli.format_size(1023) == "1023 B"
    assert cli.format_size(1536) == "1.5 KB"
    assert cli.format_size(3 * 1024**3) == "3.0 GB"


@pytest.fixture
def shown(monkeypatch):
    """Replaces the window with a record of what would be shown."""
    calls = []
    monkeypatch.setattr(
        cli, "show_browser", lambda *args, **kwargs: calls.append((args, kwargs)) or None
    )
    return calls


@pytest.fixture
def page(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<html></html>")
    return page


def test_browse_makes_a_new_folder_a_virtual_bucket_and_shows_it(
    runner, tmp_path, fake_storage, shown, page
):
    folder = tmp_path / "new"

    result = runner.invoke(
        cli.main,
        ["browse", str(folder), "--bucket", "b", "--prefix", "results", "--page", str(page)],
    )

    assert result.exit_code == 0, result.output
    assert read_bucket_info(folder) == BucketInfo("b", "results/", None)
    (virtual_bucket, storage, shown_page), options = shown[0]
    assert virtual_bucket.folder == folder.resolve()
    assert storage is fake_storage
    assert shown_page == page
    assert options == {"update_on_ready": True}


def test_browse_of_a_virtual_bucket_needs_no_bucket(runner, folder, fake_storage, shown, page):
    write_bucket_info(folder, BucketInfo("b", "results"))

    result = runner.invoke(cli.main, ["browse", str(folder), "--page", str(page)])

    assert result.exit_code == 0, result.output
    (virtual_bucket, _, _), options = shown[0]
    assert virtual_bucket.info == BucketInfo("b", "results/", None)
    assert options == {"update_on_ready": False}


def test_browse_of_a_plain_folder_needs_a_bucket(runner, folder, fake_storage, shown, page):
    result = runner.invoke(cli.main, ["browse", str(folder), "--page", str(page)])

    assert result.exit_code == 1
    assert "is not a virtual bucket. Give the bucket to make it one." in result.output
    assert shown == [] and not (folder / ".ebrains").exists()


def test_browse_of_another_bucket_is_an_error(runner, folder, fake_storage, shown, page):
    write_bucket_info(folder, BucketInfo("b"))

    result = runner.invoke(
        cli.main, ["browse", str(folder), "--bucket", "other", "--page", str(page)]
    )

    assert result.exit_code == 1
    assert "The folder is a virtual bucket of bucket 'b', not of bucket 'other'." in result.output
    assert shown == []


def test_browse_takes_the_page_from_the_environment(runner, folder, fake_storage, shown, page):
    write_bucket_info(folder, BucketInfo("b"))

    result = runner.invoke(
        cli.main, ["browse", str(folder)], env={"EBRAINS_BUCKET_SYNC_BROWSER_PAGE": str(page)}
    )

    assert result.exit_code == 0, result.output
    assert shown[0][0][2] == page


def test_browse_without_a_page_to_show_is_an_error(
    runner, folder, fake_storage, shown, monkeypatch
):
    write_bucket_info(folder, BucketInfo("b"))

    def no_page():
        raise FileNotFoundError("The browser page was not found. Give --page.")

    monkeypatch.setattr(cli, "default_page_path", no_page)

    result = runner.invoke(cli.main, ["browse", str(folder)])

    assert result.exit_code == 1
    assert "Error: The browser page was not found. Give --page." in result.output
    assert shown == []
