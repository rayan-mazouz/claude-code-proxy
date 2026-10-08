from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _assert_unix_installer_keeps_key_out_of_argv(script: str) -> None:
    assert "<BASE_URL> <API_KEY>" not in script
    assert "${2:-}" not in script
    assert "--arg key" not in script
    assert "read -r -s API_KEY < /dev/tty" in script
    assert "CLAUDE_PROXY_KEY_FILE" in script
    assert 'jq --rawfile key "$KEY_FILE"' in script


def test_public_installers_never_put_key_in_command_or_process_arguments():
    unix_script = (REPOSITORY_ROOT / "frontend/public/install.sh").read_text()
    powershell_script = (REPOSITORY_ROOT / "frontend/public/install.ps1").read_text()

    _assert_unix_installer_keeps_key_out_of_argv(unix_script)
    assert "CC_PROXY_KEY" not in powershell_script
    assert "<KEY>" not in powershell_script
    assert "$BaseUrl = $env:CC_PROXY_URL" in powershell_script
    assert "Read-Host 'API key (input hidden)' -AsSecureString" in powershell_script


def test_dashboard_generated_commands_do_not_interpolate_revealed_secret():
    users_page = (REPOSITORY_ROOT / "frontend/src/app/(dashboard)/users/page.tsx").read_text()
    commands = users_page[users_page.index("const unixCmd") : users_page.index("const installCmd")]

    assert "${secret}" not in commands
    assert "CC_PROXY_KEY" not in commands
    assert "input hidden" in users_page


def test_served_statusline_is_the_repository_copy(client):
    response = client.get("/api/v1/clients/claude-code-statusline.sh")

    assert response.status_code == 200
    assert response.text == (REPOSITORY_ROOT / "backend/clients/claude-code-statusline.sh").read_text()
    assert 'usage_tmp="${usage_cache}.tmp.$$"' in response.text
    assert "umask 077" in response.text
