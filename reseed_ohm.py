#!/usr/bin/env python3
"""Re-seed OHM from the concept data we created earlier."""
import requests, json, time

with open('/root/olympus/shared/ohm-config.json') as f:
    config = json.load(f)
token = config['agents']['metis']
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
base = "http://127.0.0.1:8710"

# All concept nodes we seeded
concepts = [
    ("concept-and-or-conversion", "AND→OR Conversion", "concept",
     "A Boolean gate where all inputs must be TRUE for the output to be TRUE. When any input converts from required (AND) to optional (OR), the gate fails open. Direction determines function: AND constraining truth (democratic escape) vs AND constraining falsehood (authoritarian trap).", 0.95, "metis-synthesis"),
    ("concept-boolean-directionality", "Boolean Directionality Principle", "concept",
     "The direction of a Boolean gate determines its function, not the operator. AND constraining truth (all conditions must be met) creates escape routes. AND constraining falsehood (all lies must hold) creates traps. The same operator, different direction, opposite outcome.", 0.94, "metis-synthesis"),
    ("concept-trap-four-mechanisms", "Trap Four Mechanisms", "concept",
     "Traps operate through four mechanisms: Interest (material benefit to trap-maker), Concealment (hiding the trap's nature), Knowledge Destruction (eliminating awareness of alternatives), and Identity (making exit psychologically impossible). Interest and Identity are primary; Concealment and Knowledge Destruction are secondary.", 0.94, "metis-synthesis"),
    ("concept-self-reinforcing-trap", "Self-Reinforcing Trap Pattern", "concept",
     "A trap where escape mechanisms are progressively disabled. Noble lie → exclusive truth → intermediary class → sacred reference → high exit cost → epistemic closure. Each step makes the next more likely. Self-sealing against challenge.", 0.94, "metis-synthesis"),
    ("concept-autocatalytic-reinforcement", "Autocatalytic Reinforcement", "concept",
     "Material feedback loops that amplify whatever direction they're pointed. War profits → investment → more war → more profits. The system reinforces itself regardless of desirability. Validated in Hormuz (war reinforcement) and Hungary (democratic reinforcement).", 0.92, "metis-synthesis"),
    ("concept-evaluation-trap", "Evaluation Trap (Goodhart's Law)", "concept",
     "Measurement becomes the target, then the sacred reference, then the only reality. Creation → Sacred Reference → Optimization → Epistemic Closure. The trap is self-reinforcing because measurement feels objective.", 0.90, "metis-synthesis"),
    ("concept-invisible-asymmetry", "Invisible Asymmetry", "concept",
     "When the measurement tool IS the asymmetry, the exploited cannot perceive exploitation. Cost reduction metrics optimize for measurable costs while invisible costs compound. The asymmetry is self-hiding.", 0.88, "metis-synthesis"),
    ("concept-expansion-liquidation-asymmetry", "Expansion-Liquidation Asymmetry", "concept",
     "Expanding takes time, capital, and biological constraints (gestation, hiring, building). Liquidating takes a decision. The asymmetry is temporal and financial. In beef: 283 days to add a calf, one day to destock. In markets: months to build positions, hours to liquidate.", 0.90, "metis-synthesis"),
    ("concept-drought-perturbation", "Drought as AND→OR Conversion", "concept",
     "Drought converts forage from an AND-gate (all cattle need it simultaneously) to an OR-gate (whoever finds it first gets it). The biology hasn't changed — the resource constraint converted the logic. Destocking is irreversible OR; supplemental feed converts the gate back to AND at high cost.", 0.90, "metis-synthesis"),
    ("concept-hormuz-autocatalytic", "Hormuz War: Autocatalytic System", "concept",
     "The Hormuz conflict is an autocatalytic text system: 10 functions scaffolding war, not ending it. Oil revenue → military spending → strategic commitments → revenue dependency. Each function reinforces the next. PGSA legitimacy emerging as the escape route.", 0.91, "metis-synthesis"),
    ("concept-hungary-andor-conversion", "Hungary AND→OR Conversion", "concept",
     "Hungary's democratic institutions are AND-gates being converted to OR-gates. AND: all checks must agree. OR: any single check suffices. Magyar's approach: make each check independently sufficent for OR, while requiring AND for democratic escape. The Boolean direction matters.", 0.92, "metis-synthesis"),
    ("concept-external-cognition", "External Cognition", "concept",
     "AI agents as System 2 prosthetics — external intelligence that augments rather than replaces human cognition. The agent doesn't think for you; it thinks with you. External cognition makes System 2 operations cheaper, making deliberate thought more accessible.", 0.93, "metis-synthesis"),
    ("concept-agent-authorization-gap", "Agent Authorization Gap", "concept",
     "Four gaps: Scope (too-broad access), Visibility (agent≠human indistinguishable), Control (flat LLM permission plane), Accountability (no single owner). 83% deploying, 29% prepared. 74% agents over-privileged. The AND-gate of proper authorization is not being constructed.", 0.88, "metis-synthesis"),
    ("concept-agent-identity-stability", "Agent Identity Stability Problem", "concept",
     "When a model changes, is it the same agent? Sakimura's insight: identity stability is a boundary condition for agent governance. Without stable identity, accountability AND-gates fail because the agent that acted is not the agent being held accountable.", 0.88, "metis-synthesis"),
    ("concept-actuarial-gap", "Actuarial Gap in Agent Liability", "concept",
     "No insurance backstop for agent AND-gates. Without actuarial data, insurers can't price risk. Without insurance, agents operate in a moral hazard zone. The AND-gate of 'agent acts AND insurance covers' fails because the second condition doesn't exist.", 0.86, "metis-synthesis"),
    ("concept-tiered-memory", "Tiered Memory Architecture", "concept",
     "Three-tier memory: hot cache (MEMORY.md, always in context), structured recall (Kuzu graph, queryable), cold archive (daily logs, searchable). Each tier trades speed for completeness. The key insight: what's in hot cache shapes attention, not what's true.", 0.87, "metis-synthesis"),
    ("concept-karpathy-autoresearch", "Karpathy Auto-Research Pattern", "concept",
     "Modify → Evaluate → Keep/Discard → Repeat. The research loop is: change something, measure the effect, keep if better, discard if not. This is System 2 externalized. The cycle time determines the learning rate.", 0.82, "metis-synthesis"),
    ("concept-exploration-vs-exploitation", "Exploration vs Exploitation: Universal Pattern", "concept",
     "The fundamental tradeoff in all adaptive systems. Exploit known rewards (efficiency) vs explore unknown possibilities (discovery). The trap: optimizing for exploitation (cost reduction) reduces exploration (innovation). The AND-gate: need BOTH exploitation AND exploration.", 0.85, "metis-synthesis"),
    ("concept-russian-oil-waiver-or-gate", "Russian Oil Sanctions Waiver: OR-Gate with Externalized Costs", "concept",
     "OR-gate: any single route to market works (direct sale, intermediary, waiver, black market). The AND-gate only works if ALL routes are blocked simultaneously — which requires coordination AND enforcement AND monitoring. The OR-gate wins because it externalizes costs.", 0.90, "metis-synthesis"),
    ("concept-hormuz-demand-rationing", "Hormuz Demand Rationing OR-Gate", "concept",
     "Demand rationing is the commercial OR-gate: any buyer who can find oil elsewhere does. The AND-gate only traps buyers dependent on Gulf oil. Physical prices $150-286/barrel vs $105 futures. Tipping point June/July if Hormuz not reopened.", 0.90, "metis-synthesis"),
    ("concept-ceps-agent-digital-identity", "CEPS: Agents Need Digital Identity", "concept",
     "CEPS policy brief: agents must have verifiable, stable digital identity. Without identity, accountability fails. The AND-gate: agent identity AND action attribution AND liability assignment. Remove identity, the whole chain collapses.", 0.85, "metis-synthesis"),
    ("concept-sailpoint-agentic-fabric", "SailPoint Agentic Fabric", "concept",
     "SailPoint/NHIMG: intent as the missing control layer. Agents need continuous authorization — not one-time grants. Identity infrastructure for AND→OR governance.", 0.90, "metis-synthesis"),
    ("concept-warsh-monetary-capture", "Warsh Confirmation: Monetary Policy AND→OR Conversion Complete", "concept",
     "Warsh sworn in May 22. 2-year Treasury >4% (YTD high). 30-year >5%. CPI 3.8%. Market vs Trump: market winning. The AND-gate of 'Fed independence AND fiscal discipline AND market confidence' has been converted to OR: any single factor drives rates.", 0.82, "metis-synthesis"),
    ("concept-perturbation-to-trap", "Perturbation to Trap Conversion", "concept",
     "How external shocks convert open systems into trapped ones. The perturbation creates urgency, urgency creates willingness to trade liberty for security, and the security apparatus becomes self-reinforcing. The AND→OR conversion: all protections required becomes any single protection sufficient.", 0.85, "metis-synthesis"),
    # New concepts from latest seeding
    ("concept-source-triangulation", "Source Triangulation", "concept",
     "Multiple independent sources converging on the same claim increase confidence beyond what any single source can provide. Triangulation is to evidence what replication is to experiment. Counter-pattern: sourcing echo chamber, where multiple sources all derive from one primary source.", 0.88, "metis-synthesis"),
    ("concept-confidence-calibration", "Confidence Calibration", "concept",
     "The degree to which an agent's stated confidence matches the actual probability of being correct. Well-calibrated agents say 80% and are right 80% of the time. Poor calibration is more dangerous than low confidence. In OHM, challenge edges provide the calibration mechanism.", 0.85, "metis-synthesis"),
    ("concept-manufactured-urgency", "Manufactured Urgency", "concept",
     "A manipulation pattern that creates artificial time pressure to prevent deliberation. The AND-gate: real urgency AND forced decision. The OR-gate escape: recognize manufactured urgency AND take time to think. Most real crises have a 24-48 hour window.", 0.87, "metis-synthesis"),
    ("concept-selective-evidence-presentation", "Selective Evidence Presentation (Cherry Picking)", "concept",
     "Presenting only evidence that supports your conclusion while suppressing contradicting evidence. Not lying — just curating. The AND-gate: viewer must AND 'this is what they showed' with 'this is all there is'. Escape: always ask 'what evidence was NOT presented?'", 0.86, "metis-synthesis"),
    ("concept-least-privilege-as-and-gate", "Least Privilege as AND-Gate", "concept",
     "Least privilege is an AND-gate: an action requires privilege AND legitimate need AND authorization AND audit trail. Removing any condition converts AND→OR. 74% of agents are over-privileged because the AND-gate was never properly constructed.", 0.89, "metis-synthesis"),
    ("concept-privilege-escalation-path", "Privilege Escalation Path", "concept",
     "A sequence of actions that converts OR-gates into AND-gates. Each step adds a constraint. The defender's goal: make every privilege an AND-gate. The attacker's goal: find the OR-gate where any single path works. Agent systems are vulnerable because LLMs don't naturally respect AND-gates.", 0.84, "metis-synthesis"),
    ("concept-feedback-loop-direction", "Feedback Loop Direction Matters", "concept",
     "Positive feedback amplifies (war profits → investment → more war). Negative feedback dampens (thermostat). The direction determines convergence or divergence. Most institutions only dampen negative feedback but never dampen positive feedback.", 0.88, "metis-synthesis"),
    ("concept-system-boundary-selection", "System Boundary Selection", "concept",
     "Where you draw the boundary determines what you see. Factory boundary: productivity. Factory+watershed boundary: pollution. The boundary IS the AND-gate on perception. This is the invisible asymmetry applied to analysis.", 0.86, "metis-synthesis"),
    ("concept-time-delay-asymmetry", "Time Delay Asymmetry", "concept",
     "Delays are not symmetric. Positive feedback with delay produces oscillation (cattle cycle). Negative feedback with delay produces instability (shower temperature). Biological time delays (283-day gestation) make expansion always slower than contraction.", 0.87, "metis-synthesis"),
    ("concept-delegated-authority-problem", "Delegated Authority Problem (Sakimura)", "concept",
     "When an agent acts on behalf of a principal, who is responsible? Creates an accountability AND-gate: principal AND agent both responsible. The OR-gate escape: neither fully responsible because the other exists. Solution: continuous authorization where each action is a new event.", 0.89, "metis-synthesis"),
    # Events
    ("event-project-freedom-hormuz", "Project Freedom: Military AND-OR Conversion", "event",
     "US military operation in Hormuz converting from AND-gate (all allies must participate) to OR-gate (any single force suffices). Commercial OR-gate emerging via demand rationing.", 0.95, "metis-synthesis"),
    # Patterns
    ("pattern-and-or-conversion-family", "AND→OR Conversion Pattern Family", "pattern",
     "A family of related patterns where Boolean AND-gates (all conditions required) are converted to OR-gates (any condition sufficient). Direction determines function: AND constraining truth (escape) vs AND constraining falsehood (trap). The same operator, opposite outcomes.", 0.94, "metis-synthesis"),
]

# Create all concept nodes
created = 0
for node_id, label, ntype, content, conf, prov in concepts:
    r = requests.post(f"{base}/node?create_only=false", headers=headers, json={
        "id": node_id, "label": label, "type": ntype,
        "content": content, "confidence": conf,
        "provenance": prov, "visibility": "team"
    }, timeout=15)
    if r.status_code in (200, 201):
        created += 1
    else:
        print(f"  ✗ {node_id}: {r.status_code} {r.text[:100]}")
    time.sleep(0.5)  # Let embeddings generate

print(f"Created {created}/{len(concepts)} concept nodes")

# Now create the edges
edges = [
    # Core AND→OR pattern family
    ("concept-and-or-conversion", "concept-boolean-directionality", "REFINES", 0.95),
    ("concept-and-or-conversion", "concept-trap-four-mechanisms", "APPLIES_TO", 0.90),
    ("concept-and-or-conversion", "concept-self-reinforcing-trap", "APPLIES_TO", 0.92),
    ("concept-and-or-conversion", "concept-autocatalytic-reinforcement", "CAUSES", 0.88),
    ("concept-boolean-directionality", "concept-trap-four-mechanisms", "APPLIES_TO", 0.87),
    ("concept-trap-four-mechanisms", "concept-self-reinforcing-trap", "CAUSES", 0.90),
    ("concept-self-reinforcing-trap", "concept-evaluation-trap", "APPLIES_TO", 0.85),
    ("concept-evaluation-trap", "concept-invisible-asymmetry", "CAUSES", 0.88),
    
    # AND→OR in domains
    ("concept-and-or-conversion", "concept-hormuz-autocatalytic", "APPLIES_TO", 0.93),
    ("concept-and-or-conversion", "concept-hungary-andor-conversion", "APPLIES_TO", 0.94),
    ("concept-and-or-conversion", "concept-drought-perturbation", "APPLIES_TO", 0.90),
    ("concept-and-or-conversion", "concept-expansion-liquidation-asymmetry", "APPLIES_TO", 0.88),
    ("concept-and-or-conversion", "concept-least-privilege-as-and-gate", "APPLIES_TO", 0.89),
    ("concept-and-or-conversion", "concept-agent-authorization-gap", "APPLIES_TO", 0.88),
    
    # Agent governance
    ("concept-agent-authorization-gap", "concept-agent-identity-stability", "CAUSES", 0.87),
    ("concept-agent-authorization-gap", "concept-actuarial-gap", "CAUSES", 0.85),
    ("concept-agent-identity-stability", "concept-actuarial-gap", "SUPPORTS", 0.83),
    ("concept-ceps-agent-digital-identity", "concept-agent-identity-stability", "SUPPORTS", 0.86),
    ("concept-sailpoint-agentic-fabric", "concept-agent-authorization-gap", "SUPPORTS", 0.88),
    
    # Systems thinking → AND→OR
    ("concept-feedback-loop-direction", "concept-autocatalytic-reinforcement", "REFINES", 0.9),
    ("concept-feedback-loop-direction", "concept-hormuz-autocatalytic", "APPLIES_TO", 0.85),
    ("concept-feedback-loop-direction", "concept-self-reinforcing-trap", "REFINES", 0.9),
    ("concept-feedback-loop-direction", "concept-expansion-liquidation-asymmetry", "APPLIES_TO", 0.88),
    ("concept-time-delay-asymmetry", "concept-autocatalytic-reinforcement", "SUPPORTS", 0.87),
    ("concept-time-delay-asymmetry", "concept-expansion-liquidation-asymmetry", "CAUSES", 0.9),
    ("concept-time-delay-asymmetry", "concept-drought-perturbation", "SUPPORTS", 0.85),
    ("concept-system-boundary-selection", "concept-invisible-asymmetry", "CAUSES", 0.88),
    ("concept-system-boundary-selection", "concept-and-or-conversion", "APPLIES_TO", 0.84),
    ("concept-system-boundary-selection", "concept-exploration-vs-exploitation", "CAUSES", 0.82),
    
    # Security
    ("concept-least-privilege-as-and-gate", "concept-agent-authorization-gap", "REFINES", 0.89),
    ("concept-least-privilege-as-and-gate", "concept-delegated-authority-problem", "SUPPORTS", 0.88),
    ("concept-privilege-escalation-path", "concept-agent-authorization-gap", "CAUSES", 0.85),
    ("concept-privilege-escalation-path", "concept-actuarial-gap", "CAUSES", 0.82),
    ("concept-privilege-escalation-path", "concept-least-privilege-as-and-gate", "CAUSES", 0.86),
    
    # Manipulation
    ("concept-manufactured-urgency", "concept-self-reinforcing-trap", "APPLIES_TO", 0.87),
    ("concept-manufactured-urgency", "concept-and-or-conversion", "APPLIES_TO", 0.85),
    ("concept-manufactured-urgency", "concept-agent-authorization-gap", "APPLIES_TO", 0.82),
    ("concept-selective-evidence-presentation", "concept-invisible-asymmetry", "CAUSES", 0.84),
    ("concept-selective-evidence-presentation", "concept-evaluation-trap", "APPLIES_TO", 0.82),
    ("concept-selective-evidence-presentation", "concept-confidence-calibration", "CAUSES", 0.84),
    
    # Cognition
    ("concept-external-cognition", "concept-tiered-memory", "SUPPORTS", 0.86),
    ("concept-external-cognition", "concept-karpathy-autoresearch", "SUPPORTS", 0.82),
    ("concept-confidence-calibration", "concept-tiered-memory", "SUPPORTS", 0.8),
    ("concept-tiered-memory", "concept-karpathy-autoresearch", "SUPPORTS", 0.82),
    
    # Governance → Delegated authority
    ("concept-delegated-authority-problem", "concept-agent-identity-stability", "CAUSES", 0.9),
    ("concept-delegated-authority-problem", "concept-agent-authorization-gap", "SUPPORTS", 0.88),
    ("concept-delegated-authority-problem", "concept-actuarial-gap", "CAUSES", 0.86),
    
    # Events
    ("event-project-freedom-hormuz", "concept-and-or-conversion", "INSTANCE_OF", 0.95),
    ("event-project-freedom-hormuz", "concept-hormuz-autocatalytic", "INSTANCE_OF", 0.93),
    ("event-project-freedom-hormuz", "concept-hormuz-demand-rationing", "CAUSES", 0.9),
    
    # Patterns
    ("pattern-and-or-conversion-family", "concept-and-or-conversion", "CONTAINS", 0.96),
    ("pattern-and-or-conversion-family", "concept-boolean-directionality", "CONTAINS", 0.94),
    
    # Hormuz specifics
    ("concept-hormuz-autocatalytic", "concept-feedback-loop-direction", "INSTANCE_OF", 0.88),
    ("concept-hormuz-demand-rationing", "concept-and-or-conversion", "INSTANCE_OF", 0.92),
    ("concept-russian-oil-waiver-or-gate", "concept-and-or-conversion", "INSTANCE_OF", 0.90),
    
    # Hungary specifics
    ("concept-hungary-andor-conversion", "concept-and-or-conversion", "INSTANCE_OF", 0.94),
    ("concept-hungary-andor-conversion", "concept-perturbation-to-trap", "INSTANCE_OF", 0.85),
    
    # Economics
    ("concept-warsh-monetary-capture", "concept-and-or-conversion", "INSTANCE_OF", 0.82),
    ("concept-warsh-monetary-capture", "concept-autocatalytic-reinforcement", "APPLIES_TO", 0.80),
    
    # Exploration
    ("concept-exploration-vs-exploitation", "concept-feedback-loop-direction", "SUPPORTS", 0.84),
]

edge_created = 0
for from_id, to_id, edge_type, confidence in edges:
    r = requests.post(f"{base}/edge", headers=headers, json={
        "from": from_id, "to": to_id, "type": edge_type,
        "confidence": confidence, "provenance": "metis-synthesis"
    }, timeout=10)
    if r.status_code in (200, 201):
        edge_created += 1
    time.sleep(0.2)

print(f"Created {edge_created}/{len(edges)} edges")

# Create tasks
tasks = [
    ("task-validate-and-or-boolean-framework", "Validate AND→OR Boolean directionality principle across domains", "P1", "socrates", "open", "2026-05-26T00:00:00Z"),
    ("task-research-actuarial-agent-risk", "Research actuarial models for agent liability and insurance gaps", "P2", "clio", "open", "2026-06-01T00:00:00Z"),
]

for tid, content, priority, assigned, status, due in tasks:
    label = content[:60]
    r = requests.post(f"{base}/node?create_only=false", headers=headers, json={
        "id": tid, "label": label, "type": "task",
        "content": content, "priority": priority,
        "task_status": status, "assigned_to": assigned,
        "due_date": due, "provenance": "metis-task"
    }, timeout=15)
    print(f"  Task {tid}: {r.status_code}")
    time.sleep(0.5)

# Link tasks to concepts
task_edges = [
    ("task-validate-and-or-boolean-framework", "concept-and-or-conversion", "REFERENCES"),
    ("task-validate-and-or-boolean-framework", "concept-boolean-directionality", "REFERENCES"),
    ("task-validate-and-or-boolean-framework", "agent-socrates", "DELEGATED_TO"),
    ("task-research-actuarial-agent-risk", "concept-actuarial-gap", "REFERENCES"),
    ("task-research-actuarial-agent-risk", "concept-agent-authorization-gap", "REFERENCES"),
    ("task-research-actuarial-agent-risk", "agent-clio", "DELEGATED_TO"),
    ("task-validate-and-or-boolean-framework", "task-research-actuarial-agent-risk", "DEPENDS_ON"),
]

for from_id, to_id, edge_type in task_edges:
    r = requests.post(f"{base}/edge", headers=headers, json={
        "from": from_id, "to": to_id, "type": edge_type,
        "confidence": 0.85, "provenance": "metis-task"
    }, timeout=10)
    print(f"  Edge {from_id} --[{edge_type}]--> {to_id}: {r.status_code}")
    time.sleep(0.2)

# Checkpoint
requests.post(f"{base}/admin/checkpoint", headers=headers, timeout=15)

# Final stats
r = requests.get(f"{base}/stats", headers=headers, timeout=10)
s = r.json()
print(f"\n✓ Final: {s['total_nodes']} nodes, {s['total_edges']} edges")
print(f"✓ Edge density: {s['total_edges']/max(s['total_nodes'],1):.1f} edges/node")