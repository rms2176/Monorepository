monorepo_prefix="$(readlink -f "$(dirname "$_")")"

[[ ":$PATH:" != *":${monorepo_prefix}/bin:"* ]] && export PATH="${monorepo_prefix}/bin:${PATH}"
[[ ":$MANPATH:" != *":${monorepo_prefix}/share/man:"* ]] && export MANPATH="${monorepo_prefix}/share/man:${MANPATH}"

unset monorepo_prefix
