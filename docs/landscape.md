# Landscape: guarding AI agents' actions (scan of 2026-10-27)

Goal: Honk is open source. The question is not "can we sell this" but "what would
people adopt, and what does nobody else do?"

Method: web search on 2026-10-27, which turned up vendor comparisons, project READMEs,
news and arXiv. **Features are as the projects describe them, not verified by running
them.** Treat this as a map, not a benchmark.

## 1. Generic agent firewalls and MCP gateways: crowded

These sit between an agent and its tools and allow, ask or block each call, with
an audit log:

| project | what it says it does |
|---|---|
| Bifrost (Maxim AI) | MCP gateway, per-key tool allow-lists, returns calls to the app for approval |
| MCPX (Lunar.dev) | tool-level RBAC, immutable audit trail |
| Docker MCP Gateway, Microsoft MCP Gateway, IBM ContextForge, MCPJungle | open-source MCP gateways |
| mcp-firewall (ressl) | policy, threat detection, approval prompt, fails closed when non-interactive |
| ActionProxy | deterministic policy, human approval, one-time execution grants |
| toolwall | argument-level gating, works as MCP and agent hooks |
| agent-firewall (two separate projects) | allow / ask / block for tools, paths, shell, domains |
| Aegis | pauses high-risk calls, tamper-evident audit trail |
| LlamaFirewall (Meta) | guardrail framework |
| Agent Approval Protocol | an open *spec* for asking approval before a tool call |

On top of that, the agent harnesses ship permissions themselves (Claude Code
permissions and hooks, for example).

Money is pouring into this space. In 2025–26 Snyk bought Invariant Labs, Check
Point bought Lakera, SentinelOne bought Prompt Security, F5 bought CalypsoAI,
Proofpoint bought Acuvity and Harvey bought Guardrails AI (Sept 2026). Several of
these deals come from one secondary source.

**Implication:** a general "allow / ask / block plus a log" guard would be project
number 15. Approval prompts and allow-lists are table stakes. Honk should
*plug into* these gateways, not compete with them.

## 2. Kubernetes-specific

- **Kubernetes MCP servers** (k8s-mcp-go, mcp-kubernetes, k8s-mcp-server and others)
  mostly offer read-only by default and opt-in write or "dangerous" tiers. The
  safety model is which tools exist, not what the change means. Approval is rare.
- **AI SRE projects** (K8sGPT, plus demos such as Kyle-Law/ai-sre-demo,
  allangelman/k8s-remediation-agent, froggychips/sre-ai-copilot) use read-only
  RBAC, GitOps PRs, server-side dry runs and human approval. The best of them check
  that *the alert cleared* after a fix.
- **Paid tools** (Komodor, Resolve and others) have guardrails, but one comparison
  says it could not find public documentation that they verify outcomes. That claim
  comes from a competing vendor.

## 3. The gap: "did it actually work, and was it a real fix?"

- Almost every tool checks whether an action was **allowed and logged**. Few check
  whether the **system ended up in the intended state**, and those that do check
  only that the alert went quiet.
- **None found check whether a "fix" is a fake fix.** Our study's bare agent
  disabled a liveness probe (`true`), created an empty ConfigMap, and deleted and
  recreated a PVC. Each one turns the service green. An "alert cleared" check calls
  all three fixed.
- Research names the same thing:
  - *Look Before You Leap* (arXiv 2609.11957) calls it **silent failure**: no
    signal fires, so nothing triggers recovery.
  - Coding-agent work (arXiv 2608.29460, unreviewed) describes agents
    **reward-hacking** by editing tests to pass, and proposes giving agents an
    *escalation channel*. With Honk, our agent escalated instead of faking a fix in
    6 of 8 scenarios, which is that idea in practice.

## What this means for Honk as open source

1. **Do not build a general firewall.** Many already exist. Publish Honk's
   approval and policy core as a small library, or adopt the Agent Approval
   Protocol, so it slots behind any of them.
2. **Own two things nobody else does:**
   - **The fake-fix detector.** Given an agent's change and the problem it claims
     to fix, flag fixes that only look healthy: probe weakened, config emptied,
     limits raised to mask a leak, destructive delete-and-recreate. Start with
     Kubernetes, where we already have ground truth.
   - **The outcome ledger.** Health at +3m, +1h and +24h, retraction, and a record
     per kind of problem. It answers "has this action ever really worked here?"
3. **Ship it where people already are:**
   - a Claude Code / agent **hook**
   - a **GitHub Action** that reviews agent-made manifest PRs
   - a **kubectl plugin**
   - the existing MCP server
4. **The study is the launch content.** "We gave an AI agent 9 broken services;
   it faked 3 fixes" is a story people share. It needs more runs, other models and
   a practitioner review before anyone publishes it.

## Sources

- [Maxim: open-source MCP gateways 2026](https://www.getmaxim.ai/articles/open-source-mcp-gateways-for-developers-in-2026/)
- [Lunar.dev: best open-source MCP gateways 2026](https://www.lunar.dev/post/the-best-open-source-mcp-gateways-in-2026)
- [ressl/mcp-firewall](https://github.com/ressl/mcp-firewall) ·
  [ActionProxy](https://github.com/ActionProxy/actionproxy) ·
  [Agent Approval Protocol](https://github.com/agentapprovalprotocol/agentapprovalprotocol) ·
  [toolwall](https://github.com/Faisal-Fayaz/toolwall) ·
  [beernathan87/agent-firewall](https://github.com/beernathan87/agent-firewall) ·
  [haseebraza715/agent-firewall](https://github.com/haseebraza715/agent-firewall) ·
  [Aegis](https://aiproductivity.ai/news/aegis-open-source-firewall-ai-agent-tool-calls/)
- [Harvey acquires Guardrails AI](https://www.harvey.ai/blog/guardrails-ai-joins-harvey) ·
  [AI security acquisition wave](https://pipelab.org/blog/ai-agent-security-acquisition-wave-2026/) ·
  [Why identity giants bought agent security firms](https://guptadeepak.com/identity-giants-bought-ai-agent-security/) ·
  [Fortune, 2026-09-16](https://fortune.com/2026/09/16/ai-agents-are-going-rogue-cios-are-racing-to-put-guardrails-around-them/)
- [Komodor: AI agent guardrails](https://komodor.com/blog/ai-agent-guardrails/) ·
  [K8sGPT](https://k8sgpt.ai/) ·
  [Kyle-Law/ai-sre-demo](https://github.com/Kyle-Law/ai-sre-demo) ·
  [allangelman/k8s-remediation-agent](https://github.com/allangelman/k8s-remediation-agent) ·
  [froggychips/sre-ai-copilot](https://github.com/froggychips/sre-ai-copilot) ·
  [LangChain: autonomous SRE agent](https://www.langchain.com/blog/how-we-build-an-autonomous-sre-agent-for-kubernetes-deployments) ·
  [DataAgent: choosing an AI SRE](https://data-agent.co/guides/choosing-ai-sre/)
- [k8s-mcp-go](https://glama.ai/mcp/servers/kaneg/k8s-mcp-go) ·
  [mcp-kubernetes](https://mcpservers.org/servers/dockndevai/mcp-kubernetes)
- [Look Before You Leap (arXiv 2609.11957)](https://arxiv.org/pdf/2609.11957) ·
  [Escalation channels vs reward hacking (2608.29460)](https://huggingface.co/papers/2608.29460.md) ·
  [Agent Control Protocol (2603.18829)](https://arxiv.org/pdf/2603.18829)
