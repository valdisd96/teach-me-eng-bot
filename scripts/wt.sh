#!/usr/bin/env bash
# wt.sh — Worktree helper for the parallel-agent workflow.
# See parallel-agentic-plan.md (Task 1).
#
# Subcommands:
#   create <branch> [slot]   — git worktree add ./worktrees/<branch> -b <branch> main,
#                              symlink .venv, ensure data/, copy env/slot<N>.env → .env,
#                              record slot in <git-dir>/wt-slot.
#   destroy <branch>         — git worktree remove ./worktrees/<branch> with prompts
#                              on uncommitted changes and on unpushed commits.
#   list                     — git worktree list with the slot annotation.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "wt: not in a git repository" >&2
    exit 1
}
WT_BASE="${REPO_ROOT}/worktrees"
ENV_DIR="${REPO_ROOT}/env"
MAX_SLOTS=2

usage() {
    cat <<EOF >&2
Usage: wt.sh <command> [args]
Commands:
  create <branch> [slot]   Create worktree at ./worktrees/<branch> (slot 1..${MAX_SLOTS}; auto if omitted)
  destroy <branch>         Remove the worktree (prompts on dirty / unpushed)
  list                     Print worktrees with slot info
EOF
    exit 1
}

slot_of() {
    # Print the slot number recorded for a worktree path, or nothing.
    local wt_path="$1"
    local gitdir
    gitdir="$(git -C "${wt_path}" rev-parse --absolute-git-dir 2>/dev/null)" || return 0
    if [[ -f "${gitdir}/wt-slot" ]]; then
        cat "${gitdir}/wt-slot"
    fi
    return 0
}

active_slots() {
    git -C "${REPO_ROOT}" worktree list --porcelain |
        awk '/^worktree / { print $2 }' |
        while read -r p; do slot_of "${p}"; done
}

next_free_slot() {
    local in_use
    in_use="$(active_slots | sort -u)"
    local s
    for ((s = 1; s <= MAX_SLOTS; s++)); do
        if ! grep -qx "${s}" <<<"${in_use}"; then
            echo "${s}"
            return 0
        fi
    done
    return 1
}

ensure_slot_env() {
    # Make sure env/slot<N>.env exists. Seed from .env.example when first created.
    local slot="$1"
    local slot_env="${ENV_DIR}/slot${slot}.env"
    mkdir -p "${ENV_DIR}"
    if [[ ! -f "${slot_env}" ]]; then
        if [[ -f "${REPO_ROOT}/.env.example" ]]; then
            cp "${REPO_ROOT}/.env.example" "${slot_env}"
        else
            : >"${slot_env}"
        fi
        echo "wt: created placeholder ${slot_env} (fill in TELEGRAM_TOKEN before running the bot)" >&2
    fi
    echo "${slot_env}"
}

build_relative_venv_target() {
    # Produce "../../.venv" with the right depth for the worktree path.
    local wt_path="$1"
    local rel="${wt_path#${REPO_ROOT}/}"
    local slashes="${rel//[^\/]/}"
    local depth=$((${#slashes} + 1))
    local prefix=""
    local i
    for ((i = 0; i < depth; i++)); do prefix+="../"; done
    echo "${prefix}.venv"
}

cmd_create() {
    local branch="${1:-}"
    local slot="${2:-}"
    [[ -n "${branch}" ]] || usage

    if [[ -z "${slot}" ]]; then
        slot="$(next_free_slot)" || {
            echo "wt: no free slot (max ${MAX_SLOTS}); destroy a worktree first." >&2
            exit 1
        }
    fi
    if [[ ! "${slot}" =~ ^[0-9]+$ ]] || ((slot < 1 || slot > MAX_SLOTS)); then
        echo "wt: slot must be an integer 1..${MAX_SLOTS}" >&2
        exit 1
    fi
    while read -r used; do
        [[ -z "${used}" ]] && continue
        if [[ "${used}" == "${slot}" ]]; then
            echo "wt: slot ${slot} already in use" >&2
            exit 1
        fi
    done < <(active_slots)

    local wt_path="${WT_BASE}/${branch}"
    if [[ -e "${wt_path}" ]]; then
        echo "wt: ${wt_path} already exists" >&2
        exit 1
    fi

    mkdir -p "$(dirname "${wt_path}")"
    git -C "${REPO_ROOT}" worktree add "${wt_path}" -b "${branch}" main

    mkdir -p "${wt_path}/data"
    local rel_target
    rel_target="$(build_relative_venv_target "${wt_path}")"
    ln -s "${rel_target}" "${wt_path}/.venv"

    local slot_env
    slot_env="$(ensure_slot_env "${slot}")"
    cp "${slot_env}" "${wt_path}/.env"

    local gitdir
    gitdir="$(git -C "${wt_path}" rev-parse --absolute-git-dir)"
    echo "${slot}" >"${gitdir}/wt-slot"

    echo "wt: created ${wt_path} (slot ${slot})"
}

cmd_destroy() {
    local branch="${1:-}"
    [[ -n "${branch}" ]] || usage

    local wt_path="${WT_BASE}/${branch}"
    if [[ ! -d "${wt_path}" ]]; then
        echo "wt: ${wt_path} does not exist" >&2
        exit 1
    fi

    local force=""
    local ans=""

    if [[ -n "$(git -C "${wt_path}" status --porcelain 2>/dev/null)" ]]; then
        echo "wt: ${branch} has uncommitted changes" >&2
        read -r -p "destroy anyway? [y/N] " ans || ans=""
        [[ "${ans}" =~ ^[Yy]$ ]] || {
            echo "wt: aborted" >&2
            exit 1
        }
        force="--force"
    fi

    if git -C "${wt_path}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
        local ahead
        ahead="$(git -C "${wt_path}" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
        if ((ahead > 0)); then
            echo "wt: ${branch} has ${ahead} unpushed commit(s)" >&2
            read -r -p "destroy anyway? [y/N] " ans || ans=""
            [[ "${ans}" =~ ^[Yy]$ ]] || {
                echo "wt: aborted" >&2
                exit 1
            }
            force="--force"
        fi
    fi

    # shellcheck disable=SC2086
    git -C "${REPO_ROOT}" worktree remove ${force} "${wt_path}"
    echo "wt: removed ${wt_path}"
}

cmd_list() {
    local path="" branch=""
    while IFS= read -r line; do
        case "${line}" in
        "worktree "*) path="${line#worktree }" ;;
        "branch refs/heads/"*) branch="${line#branch refs/heads/}" ;;
        "")
            if [[ -n "${path}" ]]; then
                local slot
                slot="$(slot_of "${path}")"
                printf "%s\t%s\tslot=%s\n" "${path}" "${branch:-detached}" "${slot:--}"
            fi
            path=""
            branch=""
            ;;
        esac
    done < <(git -C "${REPO_ROOT}" worktree list --porcelain)
    if [[ -n "${path}" ]]; then
        local slot
        slot="$(slot_of "${path}")"
        printf "%s\t%s\tslot=%s\n" "${path}" "${branch:-detached}" "${slot:--}"
    fi
}

main() {
    local cmd="${1:-}"
    [[ $# -gt 0 ]] && shift
    case "${cmd}" in
    create) cmd_create "$@" ;;
    destroy) cmd_destroy "$@" ;;
    list) cmd_list ;;
    "" | -h | --help) usage ;;
    *)
        echo "wt: unknown command '${cmd}'" >&2
        usage
        ;;
    esac
}

main "$@"
