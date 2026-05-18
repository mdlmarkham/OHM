# Medical Diagnosis Scenario

Complete agent workflow for differential diagnosis using OHM's NEGATES edges, compound confidence with correlation, and evidence chain reasoning.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **Radiologist** | Imaging interpretation (X-ray, CT, MRI) | L3 | `create_edge(CAUSES)`, `observe()` |
| **Pathologist** | Blood work and lab results | L3 | `create_edge(SUPPORTS)`, `observe()` |
| **Clinician** | Physical exam findings, vital signs | L3 | `create_edge(PREDICTS)`, `rules_out()` |
| **Diagnostician** | Differential diagnosis synthesis | L4 | `differential_diagnosis()`, `compound_confidence()` |
| **Second Opinion** | Independent review, challenge | L4 | `challenge()`, `contradictions()` |

## Workflow 1: Differential Diagnosis with NEGATES

### Step 1 — Clinician records findings

```python
import ohm.sdk as ohm

with ohm.connect("hospital.duckdb", actor="clinician") as g:
    g.register_agent(values=["patient_care", "accuracy"])

    # Create patient node
    patient = g.create_node(
        label="Patient #8472 — 45M, fever, cough, fatigue",
        node_type="concept",
        priority="P1",
    )

    # Record physical exam findings
    g.observe(
        patient["id"],
        obs_type="temperature",
        value=102.3,
        sigma=0.2,
        metadata={"unit": "F", "timestamp": "2026-05-17T09:00:00Z"},
    )
    g.observe(
        patient["id"],
        obs_type="respiratory_rate",
        value=22.0,
        sigma=1.0,
        metadata={"unit": "bpm"},
    )

    # Key negative finding: no rash
    no_rash = g.create_node(
        label="Rash Absent — No skin findings",
        node_type="concept",
    )
    g.observe(
        no_rash["id"],
        obs_type="physical_exam",
        value=1.0,  # Confirmed absent
        sigma=0.05,
    )
```

### Step 2 — Radiologist interprets imaging

```python
with ohm.connect("hospital.duckdb", actor="radiologist") as g:
    g.register_agent(values=["imaging", "diagnosis"])

    # Chest X-ray findings
    cxr_findings = g.create_node(
        label="CXR: Bilateral infiltrates — consistent with pneumonia",
        node_type="concept",
    )
    g.observe(
        cxr_findings["id"],
        obs_type="imaging_confidence",
        value=0.85,
        sigma=0.1,
    )

    g.create_edge(
        from_node=cxr_findings["id"],
        to_node=patient["id"],
        edge_type="CAUSES",
        layer="L3",
        confidence=0.85,
        metadata={"modality": "imaging"},
    )
```

### Step 3 — Pathologist reports lab results

```python
with ohm.connect("hospital.duckdb", actor="pathologist") as g:
    g.register_agent(values=["lab", "precision"])

    # Blood work
    cbc = g.create_node(
        label="CBC: WBC 14.2K, elevated neutrophils — bacterial infection",
        node_type="concept",
    )
    g.observe(
        cbc["id"],
        obs_type="lab_confidence",
        value=0.9,
        sigma=0.05,
    )

    g.create_edge(
        from_node=cbc["id"],
        to_node=patient["id"],
        edge_type="SUPPORTS",
        layer="L3",
        confidence=0.9,
        metadata={"modality": "lab"},
    )

    # Malaria smear — negative
    malaria_smear = g.create_node(
        label="Malaria Smear — Negative",
        node_type="concept",
    )
    g.observe(
        malaria_smear["id"],
        obs_type="lab_confidence",
        value=0.98,
        sigma=0.02,
    )
```

### Step 4 — Clinician rules out conditions with NEGATES

```python
with ohm.connect("hospital.duckdb", actor="clinician") as g:
    # Create candidate diagnosis nodes
    pneumonia = g.create_node(
        label="Community-Acquired Pneumonia",
        node_type="concept",
    )
    malaria = g.create_node(
        label="Malaria",
        node_type="concept",
    )
    meningitis = g.create_node(
        label="Meningitis",
        node_type="concept",
    )

    # Evidence: pneumonia explains symptoms
    g.create_edge(
        from_node=pneumonia["id"],
        to_node=patient["id"],
        edge_type="CAUSES",
        layer="L3",
        confidence=0.8,
    )

    # Evidence: malaria possible (but ruled out by smear)
    g.create_edge(
        from_node=malaria["id"],
        to_node=patient["id"],
        edge_type="PREDICTS",
        layer="L3",
        confidence=0.4,
    )

    # NEGATES: no rash rules out meningitis
    g.rules_out(
        from_node=no_rash["id"],
        to_node=meningitis["id"],
        confidence=0.85,
    )

    # NEGATES: negative smear rules out malaria
    g.rules_out(
        from_node=malaria_smear["id"],
        to_node=malaria["id"],
        confidence=0.95,
    )
```

### Step 5 — Diagnostician runs differential

```python
with ohm.connect("hospital.duckdb", actor="diagnostician") as g:
    g.register_agent(values=["diagnosis", "evidence"])

    # Run differential diagnosis
    ddx = g.differential_diagnosis(patient["id"])

    for condition in ddx:
        status = "RULED OUT" if condition["ruled_out"] else "CANDIDATE"
        print(f"{status}: {condition['label']} "
              f"(score: {condition.get('composite_score', 'N/A')})")
        if condition["ruled_out"]:
            print(f"  Ruled out by: {condition['ruled_out_by']}")

    # Compound confidence: imaging + lab are independent modalities
    imaging_obs = [
        {"confidence": 0.85, "modality": "imaging"},
    ]
    lab_obs = [
        {"confidence": 0.9, "modality": "lab"},
    ]

    # Independent modalities compound multiplicatively
    independent = g.compound_confidence(
        imaging_obs + lab_obs,
        correlation=0.0,
    )
    print(f"Independent compound: {independent['compound_confidence']:.3f}")

    # Same modality findings are correlated
    correlated = g.compound_confidence(
        [{"confidence": 0.85}, {"confidence": 0.82}],  # Two imaging reads
        correlation=0.8,  # Same modality, same radiologist
    )
    print(f"Correlated compound: {correlated['compound_confidence']:.3f}")
```

## Workflow 2: Second Opinion Challenge

```python
with ohm.connect("hospital.duckdb", actor="second_opinion") as g:
    g.register_agent(values=["review", "safety"])

    # Review the pneumonia diagnosis
    pneumonia_edges = g.query(filter_type="CAUSES", layer="L3")
    for edge in pneumonia_edges:
        if edge["to_node"] == patient["id"]:
            # Challenge: could also be viral pneumonia
            g.challenge(
                edge["id"],
                reason="CXR findings also consistent with viral pneumonia. "
                       "Recommend viral panel before antibiotics.",
                confidence=0.7,
            )

# Diagnostician checks for contradictions
with ohm.connect("hospital.duckdb", actor="diagnostician") as g:
    conflicts = g.contradictions()
    for c in conflicts:
        print(f"CONTRADICTS: {c['node_a_label']} vs {c['node_b_label']}")

    # Revised plan: order viral panel, hold antibiotics pending results
    viral_panel = g.create_node(
        label="Order: Respiratory Viral Panel",
        node_type="plan",
        priority="P0",
    )
    g.create_edge(
        from_node=viral_panel["id"],
        to_node=patient["id"],
        edge_type="PREDICTS",
        layer="L4",
        confidence=0.9,
    )
```

## Key Reasoning Primitives Used

| Primitive | Medical Use Case |
|-----------|-----------------|
| `rules_out()` / NEGATES | Negative smear rules out malaria; no rash rules out meningitis |
| `differential_diagnosis()` | Rank candidate conditions, exclude NEGATES-ruled-out |
| `compound_confidence(correlation=0.0)` | Independent modalities (imaging + lab) compound multiplicatively |
| `compound_confidence(correlation=0.8)` | Same-modality findings are correlated, don't double-count |
| `confidence_chain()` | Trace evidence from CXR → pneumonia → patient |
| `challenge()` | Second opinion challenges bacterial vs viral pneumonia |
| `contradictions()` | Surface diagnostic disagreements |
