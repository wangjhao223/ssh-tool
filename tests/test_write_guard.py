import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from remote_ws import write_guard  # noqa: E402


CONFIG = {
    "CODE_ROOT": "/srv/code",
    "DATA_ROOT": "/srv/data",
    "CONDA_ROOT": "/srv/conda",
}
CWD = "/srv/code/project"


class WriteGuardTests(unittest.TestCase):
    def scan(self, command, cwd=CWD, conda_env="science"):
        return write_guard.find_write_violations(command, cwd, conda_env, CONFIG)

    def assertBlocked(self, command, cwd=CWD):
        violations = self.scan(command, cwd)
        self.assertTrue(violations, f"expected a violation for {command!r}")

    def assertAllowed(self, command, cwd=CWD):
        violations = self.scan(command, cwd)
        self.assertEqual([], violations, f"unexpected violation for {command!r}: {violations}")

    def test_cp_target_directory_forms_blocked(self):
        for command in (
            "cp -t /etc/dest src.txt",
            "cp -t/etc/dest src.txt",
            "cp --target-directory /etc/dest src.txt",
            "cp --target-directory=/etc/dest src.txt",
            "cp -rv -t /etc/dest src.txt",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_cp_target_directory_allowed(self):
        for command in (
            "cp -t /tmp/dest src.txt",
            "cp -t/srv/data/dest src.txt",
            "cp --target-directory=/tmp/dest src.txt",
            "cp --target-directory /srv/data/dest src.txt",
            "cp -t /tmp/dest /etc/hosts",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_ln_and_install_target_directory(self):
        for command in (
            "ln -t /etc/links target.txt",
            "ln -t/etc/links target.txt",
            "ln --target-directory=/etc/links target.txt",
            "install -t /etc/bin program",
            "install -t/etc/bin program",
            "install --target-directory /etc/bin program",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in (
            "ln -t /tmp/links target.txt",
            "install -t /srv/data/bin program",
            "install --target-directory=/tmp/bin program",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_install_directory_mode(self):
        for command in ("install -d /etc/newdir", "install -dvt /etc/newdir"):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in ("install -d /tmp/newdir", "install -d /srv/data/newdir"):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_cp_last_operand_still_checked(self):
        self.assertBlocked("cp -r source /etc/dest")
        self.assertBlocked("cp /etc/hosts dest.txt", cwd="/etc")
        self.assertAllowed("cp /etc/hosts /tmp/copy")
        self.assertAllowed("cp -r source build")

    def test_git_clone_option_values_do_not_displace_target(self):
        for command in (
            "git clone -b main https://example.invalid/repo.git /etc/repo",
            "git clone --depth 1 https://example.invalid/repo.git /etc/repo",
            "git clone -bmain --depth=1 https://example.invalid/repo.git /etc/repo",
            "git clone -c core.bare=false https://example.invalid/repo.git /etc/repo",
            "git clone --branch=main -q https://example.invalid/repo.git /etc/repo",
            "git clone --recurse-submodules https://example.invalid/repo.git /etc/repo",
            "git clone --filter=blob:none https://example.invalid/repo.git /etc/repo",
            "git clone --revision v1.0 https://example.invalid/repo.git /etc/repo",
            "git clone --template /tmp/tpl https://example.invalid/repo.git /etc/repo",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_git_clone_allowed_targets(self):
        for command in (
            "git clone -b main https://example.invalid/repo.git /tmp/repo",
            "git clone --depth 1 https://example.invalid/repo.git /srv/data/repo",
            "git clone -bmain https://example.invalid/repo.git repo",
            "git clone -b main https://example.invalid/repo.git",
            "git clone --recurse-submodules https://example.invalid/repo.git /srv/code/repo",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)
        self.assertAllowed("git status")
        self.assertAllowed("git log --oneline")

    def test_git_clone_separate_git_dir(self):
        self.assertBlocked(
            "git clone --separate-git-dir /etc/gitdir "
            "https://example.invalid/repo.git /tmp/repo")
        self.assertBlocked(
            "git clone --separate-git-dir=/etc/gitdir "
            "https://example.invalid/repo.git /tmp/repo")
        self.assertAllowed(
            "git clone --separate-git-dir=/tmp/gitdir "
            "https://example.invalid/repo.git /srv/data/repo")

    def test_sed_short_option_clusters_blocked(self):
        for command in (
            "sed -Ei 's/a/b/' /etc/passwd",
            "sed -ni 's/a/b/p' /etc/passwd",
            "sed -i.bak 's/a/b/' /etc/passwd",
            "sed -E -i 's/a/b/' /etc/passwd",
            "sed -r -i 's/a/b/' /etc/passwd",
            "sed -ni -e 's/a/b/p' /etc/passwd",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_sed_in_place_allowed_paths(self):
        for command in (
            "sed -Ei 's/a/b/' app.conf",
            "sed -ni 's/a/b/p' app.conf",
            "sed -i.bak 's/a/b/' /tmp/app.conf",
            "sed -E -i 's/a/b/' /srv/data/app.conf",
            "sed -Ei 's/a/b/'",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_sed_script_options_are_not_write_targets(self):
        for command in (
            "sed -i -e 's/a/b/' /etc/passwd",
            "sed -i --expression='s/a/b/' /etc/passwd",
            "sed -i --expression 's/a/b/' /etc/passwd",
            "sed -i -f /tmp/script.sed /etc/passwd",
            "sed -i --file=/tmp/script.sed /etc/passwd",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in (
            "sed -i -e 's/a/b/' app.conf",
            "sed -i -f /etc/script.sed app.conf",
            "sed -i --file=/tmp/script.sed app.conf",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_sed_long_in_place(self):
        for command in (
            "sed --in-place 's/a/b/' /etc/passwd",
            "sed --in-place=.bak 's/a/b/' /etc/passwd",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in (
            "sed --in-place 's/a/b/' app.conf",
            "sed --in-place=.bak 's/a/b/' /tmp/passwd",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_sed_without_in_place_is_read_only(self):
        for command in (
            "sed -E 's/a/b/' /etc/passwd",
            "sed -n 'p' /etc/passwd",
            "sed --quiet 'p' /etc/passwd",
            "sed --line-length 10 'p' /etc/passwd",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_unzip_compact_destination_blocked(self):
        for command in (
            "unzip -d/etc/repo app.zip",
            "unzip -d/etc/repo",
            "unzip -od/etc/repo app.zip",
            "unzip -qd/etc/repo app.zip",
            "unzip -q -d /etc/repo app.zip",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_unzip_destination_allowed(self):
        for command in (
            "unzip -drepo app.zip",
            "unzip -d /tmp/repo app.zip",
            "unzip -qd/srv/data/repo app.zip",
            "unzip -- app.zip -d/tmp/ignored",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_unzip_without_destination_uses_cwd(self):
        self.assertAllowed("unzip app.zip")
        self.assertAllowed("unzip /etc/app.zip")
        self.assertBlocked("unzip app.zip", cwd="/etc")

    def test_curl_output_options_blocked(self):
        for command in (
            "curl -D/etc/headers https://example.invalid/file",
            "curl -D /etc/headers https://example.invalid/file",
            "curl --dump-header=/etc/headers https://example.invalid/file",
            "curl --dump-header /etc/headers https://example.invalid/file",
            "curl -c/etc/cookies https://example.invalid/file",
            "curl -c /etc/cookies https://example.invalid/file",
            "curl --cookie-jar=/etc/cookies https://example.invalid/file",
            "curl --cookie-jar /etc/cookies https://example.invalid/file",
            "curl --trace /etc/trace https://example.invalid/file",
            "curl --trace=/etc/trace https://example.invalid/file",
            "curl --trace-ascii=/etc/trace https://example.invalid/file",
            "curl --trace-ascii /etc/trace https://example.invalid/file",
            "curl --stderr /etc/curl.err https://example.invalid/file",
            "curl --etag-save=/etc/etags https://example.invalid/file",
            "curl --hsts /etc/hsts https://example.invalid/file",
            "curl --alt-svc=/etc/alt-svc https://example.invalid/file",
            "curl --libcurl /etc/curl.c https://example.invalid/file",
            "curl -o/etc/body https://example.invalid/file",
            "curl -o /etc/body https://example.invalid/file",
            "curl --output=/etc/body https://example.invalid/file",
            "curl --output /etc/body https://example.invalid/file",
            "curl --output-dir=/etc/body https://example.invalid/file",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_curl_output_options_allowed(self):
        for command in (
            "curl -D/tmp/headers https://example.invalid/file",
            "curl -D /tmp/headers https://example.invalid/file",
            "curl --dump-header=/tmp/headers https://example.invalid/file",
            "curl -c/tmp/cookies https://example.invalid/file",
            "curl --cookie-jar /tmp/cookies https://example.invalid/file",
            "curl --trace /tmp/trace https://example.invalid/file",
            "curl --trace-ascii=/tmp/trace https://example.invalid/file",
            "curl --stderr /tmp/curl.err https://example.invalid/file",
            "curl --etag-save=/tmp/etags https://example.invalid/file",
            "curl --hsts /tmp/hsts https://example.invalid/file",
            "curl --alt-svc=/tmp/alt-svc https://example.invalid/file",
            "curl --libcurl /tmp/curl.c https://example.invalid/file",
            "curl -o/tmp/body https://example.invalid/file",
            "curl --output=/tmp/body https://example.invalid/file",
            "curl --output /tmp/body https://example.invalid/file",
            "curl --output-dir /tmp/body https://example.invalid/file",
            "curl -D - https://example.invalid/file",
            "curl --trace-ascii - https://example.invalid/file",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_wget_output_options_blocked(self):
        for command in (
            "wget -P/etc/dest https://example.invalid/file",
            "wget -P /etc/dest https://example.invalid/file",
            "wget --directory-prefix=/etc/dest https://example.invalid/file",
            "wget --directory-prefix /etc/dest https://example.invalid/file",
            "wget -o/etc/wget.log https://example.invalid/file",
            "wget -o /etc/wget.log https://example.invalid/file",
            "wget --output-file=/etc/wget.log https://example.invalid/file",
            "wget --output-file /etc/wget.log https://example.invalid/file",
            "wget -a/etc/wget.log https://example.invalid/file",
            "wget -a /etc/wget.log https://example.invalid/file",
            "wget --append-output=/etc/wget.log https://example.invalid/file",
            "wget --append-output /etc/wget.log https://example.invalid/file",
            "wget --save-cookies=/etc/cookies https://example.invalid/file",
            "wget --rejected-log /etc/rejects https://example.invalid/file",
            "wget --warc-file=/etc/warc https://example.invalid/file",
            "wget -O/etc/body https://example.invalid/file",
            "wget -O /etc/body https://example.invalid/file",
            "wget --output-document=/etc/body https://example.invalid/file",
            "wget --output-document /etc/body https://example.invalid/file",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_wget_output_options_allowed(self):
        for command in (
            "wget -P/tmp/dest https://example.invalid/file",
            "wget -P /tmp/dest https://example.invalid/file",
            "wget --directory-prefix=/tmp/dest https://example.invalid/file",
            "wget --directory-prefix /tmp/dest https://example.invalid/file",
            "wget -o/tmp/wget.log https://example.invalid/file",
            "wget -o /tmp/wget.log https://example.invalid/file",
            "wget --output-file=/tmp/wget.log https://example.invalid/file",
            "wget -a/tmp/wget.log https://example.invalid/file",
            "wget --append-output /tmp/wget.log https://example.invalid/file",
            "wget --save-cookies=/tmp/cookies https://example.invalid/file",
            "wget --rejected-log /tmp/rejects https://example.invalid/file",
            "wget --warc-file /tmp/warc https://example.invalid/file",
            "wget -O/tmp/body https://example.invalid/file",
            "wget --output-document=/tmp/body https://example.invalid/file",
            "wget --output-document /tmp/body https://example.invalid/file",
            "wget -O - https://example.invalid/file",
            "wget -o - https://example.invalid/file",
            "wget -a - https://example.invalid/file",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_timeout_options_blocked(self):
        for command in (
            "timeout -s KILL 10 sudo rm -rf /etc",
            "timeout -s KILL 10 rm -rf /etc",
            "timeout -sKILL 10 rm -rf /etc",
            "timeout --signal KILL 10 rm -rf /etc",
            "timeout --signal=KILL 10 rm -rf /etc",
            "timeout -k 5 10 rm -rf /etc",
            "timeout -k5 10 rm -rf /etc",
            "timeout --kill-after=5 10 rm -rf /etc",
            "timeout --kill-after 5 10 rm -rf /etc",
            "timeout -s KILL -k 5 10 git clone https://example.invalid/repo.git /etc/repo",
            "timeout -s KILL 10 bash -c 'rm -rf /etc'",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_timeout_normal_commands_allowed(self):
        for command in (
            "timeout 10 python train.py",
            "timeout -s TERM 30 python train.py",
            "timeout -sTERM 30 python train.py",
            "timeout --signal=TERM --kill-after=5 30 python train.py",
            "timeout --signal TERM --kill-after 5 30 python train.py",
            "timeout -k 5 30 python train.py",
            "timeout -k5 30 python train.py",
            "timeout -s KILL 10 bash train.sh",
            "timeout -s KILL 10 bash train.sh -c 4",
            "timeout 30 python train.py -c config",
            "timeout -s TERM 30 python -m pytest tests -c setup.cfg",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_inline_shell_requires_confirmation(self):
        for command in (
            "bash -c 'echo hi'",
            "/bin/bash -c 'echo hi'",
            "sh -c 'echo hi'",
            "dash -c 'echo hi'",
            "zsh -c 'echo hi'",
            "bash -lc 'echo hi'",
            "bash --norc -c 'echo hi'",
            "bash -Oextglob -c 'echo hi'",
            "bash -O extglob -c 'echo hi'",
            "bash -o pipefail -c 'echo hi'",
            "bash --rcfile /tmp/rc -c 'echo hi'",
            "ksh -c 'echo hi'",
            "fish -c 'echo hi'",
            "tcsh -c 'echo hi'",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in (
            "bash train.sh",
            "sh scripts/train.sh",
            "bash --norc train.sh",
            "bash -Oextglob train.sh",
            "bash -O extglob train.sh",
            "bash train.sh -c 4",
            "bash -x train.sh -c 4",
            "zsh -l",
            "ksh scripts/train.ksh",
            "fish scripts/train.fish",
            "sh scripts/train.sh -c 4",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_inline_interpreters_require_confirmation(self):
        for command in (
            "python -c 'print(1)'",
            "python3.11 -c 'print(1)'",
            "pypy3 -c 'print(1)'",
            "/srv/conda/bin/python -B -c 'print(1)'",
            "perl -e 'print 1'",
            "perl -ne 'print' /etc/passwd",
            "perl -E 'say 1'",
            "ruby -e 'puts 1'",
            "node -e 'console.log(1)'",
            "node --eval='console.log(1)'",
            "node -p '1+1'",
            "php -r 'echo 1;'",
            "python -Werror::DeprecationWarning -c 'print(1)'",
            "python -W error -c 'print(1)'",
            "python -X dev -c 'print(1)'",
            "python --check-hash-based-pycs always -c 'print(1)'",
            "perl -MDevel::Cover -e 'print 1'",
            "perl -I lib -e 'print 1'",
            "ruby -rsomegem -e 'puts 1'",
            "ruby -I lib -e 'puts 1'",
            "ruby -F , -e 'puts 1'",
            "ruby --encoding utf-8 -e 'puts 1'",
            "node -rfoo -e 'console.log(1)'",
            "node -r foo -e 'console.log(1)'",
            "node --require foo --eval='console.log(1)'",
            "php -ddisplay_errors=1 -r 'echo 1;'",
            "php -d display_errors=1 -r 'echo 1;'",
            "php --define foo=bar -r 'echo 1;'",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_inline_ignores_script_arguments(self):
        for command in (
            "python train.py -c config",
            "python train.py -Werror",
            "python -m pytest tests -c setup.cfg",
            "python -m pytest -c setup.cfg tests",
            "python -m torch.distributed.run train.py -c config",
            "bash train.sh -c 4",
            "bash -x train.sh -c 4",
            "sh script.sh -c 4",
            "fish script.fish -c 4",
            "perl script.pl -e foo",
            "ruby script.rb -e foo",
            "node script.js -e foo",
            "php script.php -r foo",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_normal_python_training_commands_allowed(self):
        for command in (
            "python train.py",
            "python3.11 -B train.py --epochs 10",
            "python -m torch.distributed.run train.py",
            "python -u train.py",
            "python -W ignore train.py",
            "python -Werror::DeprecationWarning train.py",
            "python -Ximporttime train.py",
            "python train.py -c config",
            "python -m pytest tests -c setup.cfg",
            "perl -MDevel::Cover script.pl",
            "ruby -rsomegem script.rb",
            "node -rfoo script.js",
            "php -ddisplay_errors=1 script.php",
            "bash -Oextglob train.sh",
            "pytest tests",
            "cd /srv/code/project && python train.py",
            "cd /srv/code/project && python train.py > /srv/data/logs/train.log 2>&1",
            "python train.py > /tmp/train.log",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_xargs_and_make_require_confirmation(self):
        for command in (
            "find . -name '*.py' | xargs grep needle",
            "printf x | xargs -n1 echo",
            "make -j8",
            "make -C /tmp",
            "gmake",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in ("echo xargs", "grep make file.txt", "ls | cat"):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_env_chdir_requires_confirmation(self):
        for command in (
            "env -C /tmp python train.py",
            "env -C/tmp python train.py",
            "env --chdir /tmp python train.py",
            "env --chdir=/tmp python train.py",
            "env -i -C /tmp make",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)
        for command in (
            "env FOO=1 python train.py",
            "env python train.py",
            "/usr/bin/env python train.py",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_dynamic_write_targets_blocked(self):
        for command in (
            "rm -rf $DIR/cache",
            "rm -rf build/*",
            "rm -rf /tmp/build/*",
            "rm -rf ./cache/?",
            "rm -rf cache/[ab]",
            "rm -rf /tmp/{../etc,x}",
            "rm -rf /tmp/{a,b}",
            "touch {a,b}",
            "cp src {a,b}",
            "echo hi > /tmp/{a,b}",
            "touch `mktemp`",
            "cp src \"$DEST\"",
            "cp -t \"$DEST\" src",
            "echo hi > $LOG",
            "echo hi >> \"$LOG\"",
            "tee $OUT",
            "dd of=$OUT",
            "unzip -d $DEST app.zip",
            "git clone https://example.invalid/repo.git $DEST",
            "sed -Ei 's/a/b/' $FILE",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_read_only_dynamic_arguments_allowed(self):
        for command in (
            "ls $DIR",
            "cat \"$FILE\"",
            "grep \"$PATTERN\" *.log",
            "echo $HOME",
            "echo {a,b}",
            "ls /tmp/{a,b}",
            "cd $DIR && ls",
            "find . -name '*.py'",
            "unzip -l \"$ARCHIVE\"",
            "python train.py --out $DIR",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_static_allowed_roots(self):
        for command in (
            "rm -rf /tmp/cache",
            "rm -rf /var/tmp/cache",
            "rm -rf /dev/shm/cache",
            "rm -rf /srv/data/jobs/run1",
            "rm -rf /srv/code/project/build",
            "mkdir -p /tmp/work",
            "install -d /tmp/newdir",
            "touch /srv/code/project/file.txt",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_static_outside_roots_blocked(self):
        for command in (
            "rm -rf /etc/cache",
            "mkdir /home/other/x",
            "touch /root/x",
            "truncate -s0 /etc/x",
            "chmod 777 /etc/x",
            "dd of=/etc/x",
            "install -d /etc/x",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_redirection_targets(self):
        for command in (
            "echo hi > /tmp/log",
            "echo hi >> /srv/data/log",
            "echo hi 2>/dev/null",
            "echo hi &>/tmp/log",
            "cat /etc/passwd > /srv/data/copy",
            "cat /etc/passwd > copy",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)
        for command in (
            "echo hi > /etc/log",
            "echo hi 2> /var/log/x",
            "echo hi > /root/log",
        ):
            with self.subTest(command=command):
                self.assertBlocked(command)

    def test_cwd_outside_blocks_relative_write(self):
        self.assertBlocked("touch file", cwd="/etc")
        self.assertBlocked("cp /tmp/a .", cwd="/etc")
        self.assertAllowed("rm -rf build")

    def test_tee_and_piped_shell(self):
        self.assertBlocked("echo hi | tee /etc/f")
        self.assertBlocked("echo hi | bash")
        self.assertBlocked("echo hi | python -c 'import sys'")
        self.assertAllowed("echo hi | tee /tmp/f")
        self.assertAllowed("echo hi | cat")

    def test_pip_install_regression(self):
        self.assertAllowed("pip install requests")
        self.assertAllowed("pip install --user requests")
        self.assertAllowed("python -m pip install requests")
        self.assertTrue(self.scan("pip install requests", conda_env=None))
        self.assertTrue(self.scan("pip install requests", conda_env=""))

    def test_docstring_declares_heuristic_not_sandbox(self):
        text = (write_guard.__doc__ or "") + (
            write_guard.find_write_violations.__doc__ or "")
        self.assertIn("启发式", text)
        self.assertIn("不是安全沙箱", text)

    def test_confirm_text(self):
        text = write_guard.confirm_text(["a", "b", "c", "d", "e", "f"])
        self.assertIn("confirmed=true", text)
        self.assertIn("等 6 处", text)


if __name__ == "__main__":
    unittest.main()