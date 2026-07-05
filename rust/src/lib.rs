use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rand::{Rng, SeedableRng};
use rand::rngs::StdRng;
use rustc_hash::FxHashMap;

/// Monte Carlo simulation of failure propagation with two-stage sampling.
///
/// Args:
///     adjacency: {node_id: [(target, confidence, probability), ...]}
///     source: Starting node ID.
///     trials: Number of Monte Carlo trials.
///     depth: Maximum BFS depth per trial.
///     seed: Optional random seed (u64).
///
/// Returns:
///     (impact_counts: {node_id: count}, per_trial_totals: [int, ...])
#[pyfunction]
fn monte_carlo_sim(
    py: Python<'_>,
    adjacency: &Bound<'_, PyDict>,
    source: &str,
    trials: usize,
    depth: usize,
    seed: Option<u64>,
) -> PyResult<PyObject> {
    // Convert the Python adjacency dict into a Rust HashMap for fast lookup.
    let mut adj: FxHashMap<String, Vec<(String, f64, f64)>> = FxHashMap::default();
    for (key, value) in adjacency.iter() {
        let from_node: String = key.extract()?;
        let edges: Vec<(String, f64, f64)> = value.extract().unwrap_or_default();
        adj.insert(from_node, edges);
    }

    let mut rng = match seed {
        Some(s) => StdRng::seed_from_u64(s),
        None => StdRng::from_entropy(),
    };

    let mut impact_counts: FxHashMap<String, u64> = FxHashMap::default();
    let mut per_trial_totals: Vec<u64> = Vec::with_capacity(trials);

    for _ in 0..trials {
        let mut visited: FxHashMap<String, bool> = FxHashMap::default();
        let mut frontier: Vec<String> = vec![source.to_string()];
        let mut affected_this_sim: u64 = 0;

        for _ in 0..depth {
            let mut next_frontier: Vec<String> = Vec::new();
            for current in frontier.iter() {
                if *visited.get(current).unwrap_or(&false) {
                    continue;
                }
                visited.insert(current.clone(), true);

                if let Some(edges) = adj.get(current) {
                    for (target, conf, prob) in edges.iter() {
                        if *visited.get(target).unwrap_or(&false) {
                            continue;
                        }
                        // Stage 1: Edge existence (confidence)
                        if rng.gen::<f64>() < *conf {
                            // Stage 2: Effect propagation (probability)
                            if rng.gen::<f64>() < *prob {
                                next_frontier.push(target.clone());
                                *impact_counts.entry(target.clone()).or_insert(0) += 1;
                                affected_this_sim += 1;
                            }
                        }
                    }
                }
            }
            frontier = next_frontier;
            if frontier.is_empty() {
                break;
            }
        }

        per_trial_totals.push(affected_this_sim);
    }

    // Build the return tuple: (dict[str, int], list[int])
    let counts_dict = PyDict::new(py);
    for (node, count) in &impact_counts {
        counts_dict.set_item(node, *count)?;
    }

    let totals_list = PyList::new(py, per_trial_totals.iter().map(|&v| v))?;

    let tuple = (counts_dict, totals_list).into_pyobject(py)?;
    Ok(tuple.into())
}

#[pymodule]
fn _mc_rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(monte_carlo_sim, m)?)?;
    Ok(())
}