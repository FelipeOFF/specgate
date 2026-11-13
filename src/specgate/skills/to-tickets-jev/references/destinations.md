# Destinations

GitHub, GitLab, Jira, Beads, local documents, and a free-form workflow are
project choices handled by the authorized harness adapter. Read
`.specgate/tracker.json` (`{"destination":"github"}`). A missing file keeps
GitHub. GitLab, Jira, and Beads publish only through an injected adapter.
Local documents and the free-form workflow do not use tracker credentials.
The MCP stores the graph, origins, revisions, and public references; it does
not require tracker credentials or silently choose a destination.
