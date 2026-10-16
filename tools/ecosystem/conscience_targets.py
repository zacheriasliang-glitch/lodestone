"""Bounded mutation-operand inspection for the advisory Conscience harness.

This recognizes a small command grammar, not arbitrary program effects. Unknown
programs remain outside that grammar. Unsupported forms of recognized mutations
or shell commands are reported as uncertain; callers must not silently allow
them. Reading directory/symlink metadata does not freeze it against later changes.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex


SHELLS = {"sh", "bash", "zsh", "dash"}
MUTATORS = {"cp", "mv", "rm", "tee", "rsync", "install", "dd", "truncate", "unlink", "shred"}
SHELL_RESERVED_PREFIXES = {
    "!", "if", "then", "else", "elif", "fi", "do", "done", "case", "esac",
    "while", "until", "for", "in", "time", "function", "select", "coproc",
    "repeat", "foreach", "end", "noglob", "nocorrect",
}


def inspect_mutations(argv, cwd, *, _depth=0):
    """Return writes, targets, destructive_sources and uncertain (all JSON types).

    Paths include both lexical absolute and resolved spellings when different,
    so an operation on a symlink entry does not lose the entry's own location.
    targets includes removal/move sources as well as write destinations.
    """
    result = {"writes": False, "targets": [], "destructive_sources": [], "uncertain": []}
    argv = list(argv)
    cwd = Path(os.path.abspath(os.path.expanduser(str(cwd))))
    if not argv:
        return result

    def uncertain(reason):
        result["writes"] = True
        if reason not in result["uncertain"]:
            result["uncertain"].append(reason)

    def path(value, destructive=False):
        if not value:
            uncertain("empty path operand")
            return
        operand = Path(value)
        absolute_operand = operand if operand.is_absolute() else cwd / operand
        lexical = Path(os.path.abspath(absolute_operand))
        spellings = [str(lexical)]
        try:
            # Resolve before lexical '..' normalization: link/../name follows
            # the link's parent, which need not be the lexical parent.
            resolved = str(absolute_operand.resolve(strict=False))
            if resolved not in spellings:
                spellings.append(resolved)
        except (OSError, RuntimeError) as exc:
            uncertain("cannot resolve path: " + str(exc))
        for spelling in spellings:
            if spelling not in result["targets"]:
                result["targets"].append(spelling)
            if destructive and spelling not in result["destructive_sources"]:
                result["destructive_sources"].append(spelling)

    if _depth > 4 or len(argv) > 512 or sum(len(argument) for argument in argv) > 65536:
        uncertain("mutation inspection bound exceeded")
        return result
    name = Path(argv[0]).name
    if name in SHELLS:
        if len(argv) != 3 or argv[1] != "-c":
            uncertain("only shell -c with one script and no extra arguments is inspected")
            return result
        try:
            command, redirects = _simple_shell(argv[2])
        except ValueError as exc:
            uncertain(str(exc))
            return result
        if command:
            inner = inspect_mutations(command, cwd, _depth=_depth + 1)
            for key in ("targets", "destructive_sources", "uncertain"):
                result[key].extend(inner[key])
            result["writes"] = inner["writes"]
        for destination in redirects:
            result["writes"] = True
            path(destination)
        return result

    if name not in MUTATORS:
        # These wrappers can conceal another command. Other programs, including
        # interpreters and Git, remain governed by the harness's other rules.
        if name in {"env", "sudo", "doas", "command", "exec", "xargs", "eval", "source", ".", "builtin", "time"}:
            uncertain("command wrapper is outside the mutation grammar: " + name)
        return result

    result["writes"] = True
    if name == "dd":
        outputs = []
        allowed = {"if", "of", "ibs", "obs", "bs", "cbs", "skip", "seek", "count", "conv", "iflag", "oflag", "status"}
        for argument in argv[1:]:
            key, separator, value = argument.partition("=")
            if not separator or key not in allowed:
                uncertain("unsupported dd operand: " + argument)
            elif key == "of":
                outputs.append(value)
        for output in outputs:
            path(output)
        if len(outputs) > 1:
            uncertain("multiple dd output operands")
        if not outputs and not result["uncertain"]:
            result["writes"] = False  # stdout is handled separately by shell redirections.
        return result

    short_flags = {
        "cp": "aRrpPLHfinvX", "mv": "finv", "rm": "firdRvIP", "tee": "aip",
        "rsync": "arvzhcptlDHiq", "install": "dCpsv", "truncate": "co", "unlink": "", "shred": "fuvz",
    }
    short_values = {
        "cp": {"t"}, "mv": {"t"}, "install": {"m", "o", "g", "S", "t"},
        "truncate": {"s", "r"}, "shred": {"n", "s"},
    }
    long_flags = {
        "cp": {"archive", "recursive", "force", "interactive", "no-clobber", "verbose", "no-target-directory"},
        "mv": {"force", "interactive", "no-clobber", "verbose", "no-target-directory"},
        "rm": {"force", "interactive", "recursive", "dir", "verbose"},
        "tee": {"append", "ignore-interrupts"},
        "rsync": {"archive", "recursive", "verbose", "compress", "checksum", "perms", "times", "links", "hard-links", "itemize-changes", "quiet"},
        "install": {"directory", "compare", "preserve-timestamps", "strip", "verbose", "no-target-directory"},
        "truncate": {"no-create", "io-blocks"}, "unlink": set(), "shred": {"force", "remove", "verbose", "zero"},
    }
    long_values = {
        "cp": {"target-directory"}, "mv": {"target-directory"},
        "install": {"mode", "owner", "group", "suffix", "target-directory"},
        "truncate": {"size", "reference"}, "shred": {"iterations", "size"},
    }
    operands, target_directory = [], None
    directory_mode = no_target_directory = False
    arguments = iter(argv[1:])
    after_separator = False
    for argument in arguments:
        if after_separator or argument == "-" or not argument.startswith("-"):
            operands.append(argument)
            continue
        if argument == "--":
            after_separator = True
            continue
        if argument.startswith("--"):
            option, equal, value = argument[2:].partition("=")
            if option in long_values.get(name, set()):
                value = value if equal else next(arguments, None)
                if value is None:
                    uncertain("missing option value: " + argument)
                elif option == "target-directory":
                    target_directory = value
            elif option in long_flags[name] and not equal:
                directory_mode |= option == "directory"
                no_target_directory |= option == "no-target-directory"
            else:
                uncertain("unsupported option: " + argument)
            continue
        flags = argument[1:]
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag in short_values.get(name, set()):
                value = flags[index + 1:] or next(arguments, None)
                if value is None:
                    uncertain("missing option value: -" + flag)
                elif flag == "t":
                    target_directory = value
                break
            if flag == "T" and name in {"cp", "mv", "install"}:
                no_target_directory = True
            elif flag in short_flags[name]:
                directory_mode |= name == "install" and flag == "d"
            else:
                uncertain("unsupported option: -" + flag)
            index += 1

    if name in {"cp", "mv", "rsync", "install"} and not directory_mode:
        if target_directory is not None:
            sources, destination = operands, target_directory
            if no_target_directory:
                uncertain("conflicting target-directory options")
        elif len(operands) >= 2:
            sources, destination = operands[:-1], operands[-1]
        else:
            uncertain("copy/move requires source and destination operands")
            return result
        if not sources:
            uncertain("no source operands")
        if name == "rsync" and any(":" in operand for operand in [*sources, destination]):
            uncertain("remote rsync operands are outside the local path grammar")
            return result
        destination_path = Path(destination)
        if not destination_path.is_absolute():
            destination_path = cwd / destination_path
        is_directory = target_directory is not None or destination.endswith("/") or destination_path.is_dir()
        if no_target_directory:
            is_directory = False
        if not is_directory:
            path(destination)
        if len(sources) > 1 and not is_directory:
            uncertain("multiple sources without an established directory destination")
        for source in sources:
            if name == "mv":
                path(source, destructive=True)
            if is_directory:
                path(str(destination_path / Path(source.rstrip("/")).name))
            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = cwd / source_path
            if source.endswith("/") or source_path.is_dir():
                uncertain("directory-source traversal is outside the mutation grammar")
    else:
        if not operands and name != "tee":
            uncertain("missing mutation target")
        if name == "unlink" and len(operands) != 1:
            uncertain("unlink requires exactly one target")
        for operand in operands:
            path(operand, destructive=name in {"rm", "unlink", "shred"})
        if name == "tee" and not operands and not result["uncertain"]:
            result["writes"] = False
    return result


def _simple_shell(script):
    """Parse only simple shell words and > / >> / < redirections.

    Non-POSIX shlex keeps whole-word quotes, allowing a quoted '>' to remain
    data. Mixed quoting, escapes, expansion and compound syntax are rejected
    rather than approximated. The direct argv path never uses this lexer.
    """
    if "\n" in script or "\r" in script or "\\" in script:
        raise ValueError("shell newlines or escapes are outside the simple-command grammar")
    quote = None
    boundaries = " \t;&|<>()"
    for index, character in enumerate(script):
        if quote:
            if character == quote:
                quote = None
                if index + 1 < len(script) and script[index + 1] not in boundaries:
                    raise ValueError("mixed quoted shell words are outside the simple-command grammar")
        elif character in "'\"":
            if index and script[index - 1] not in boundaries:
                raise ValueError("mixed quoted shell words are outside the simple-command grammar")
            quote = character
    lexer = shlex.shlex(script, posix=False, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = []
    for token in lexer:
        quoted = token.startswith(("'", '"'))
        if quoted:
            if len(token) < 2 or token[-1] != token[0]:
                raise ValueError("incomplete quoted shell word")
            value = token[1:-1]
            if token[0] == '"' and any(mark in value for mark in ("$", "`")):
                raise ValueError("shell substitution is outside the simple-command grammar")
        else:
            value = token
            if any(mark in token for mark in ("'", '"', "$", "`", "*", "?", "[", "]", "{", "}", "#")):
                raise ValueError("shell expansion or mixed quoting is outside the simple-command grammar")
            if token.startswith("~"):
                value = os.path.expanduser(token)
        words.append((value, quoted))
    command, redirects = [], []
    index = 0
    while index < len(words):
        word, quoted = words[index]
        if not quoted and word in {">", ">>", "<"}:
            if index and words[index - 1][0].isdigit() and not words[index - 1][1]:
                raise ValueError("explicit shell file descriptors are outside the simple-command grammar")
            if index + 1 >= len(words):
                raise ValueError("redirection has no destination")
            target, target_quoted = words[index + 1]
            if not target_quoted and any(character in target for character in ";&|<>()"):
                raise ValueError("unsupported redirection destination")
            if word != "<":
                redirects.append(target)
            index += 2
            continue
        if not quoted and any(character in word for character in ";&|<>()"):
            raise ValueError("compound shell syntax is outside the simple-command grammar")
        if not command and not quoted and word in SHELL_RESERVED_PREFIXES:
            raise ValueError("shell reserved-command prefix is outside the simple-command grammar: " + word)
        if not command and "=" in word:
            raise ValueError("shell assignment is outside the simple-command grammar")
        command.append(word)
        index += 1
    return command, redirects
