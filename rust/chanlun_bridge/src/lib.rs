use std::collections::{BTreeMap, BTreeSet};

use chanlun_lean_lib::{guidao, zhongshu, ChanlunPipeline, SecondMarker};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

pub const SCHEMA_VERSION: &str = "chanlun_bridge/v1";

#[derive(Debug, Deserialize)]
pub struct BatchRequest {
    /// When true, replay every daily prefix and return each marker at the first prefix where
    /// it became visible. This is the no-look-ahead mode used for historical validation.
    #[serde(default)]
    pub replay: bool,
    /// Recompute only the most recent N daily prefixes. Mutually exclusive with replay.
    #[serde(default)]
    pub recent_sessions: Option<usize>,
    #[serde(default)]
    pub bars: Vec<BarInput>,
    #[serde(default)]
    pub items: Vec<ItemInput>,
}

#[derive(Debug, Deserialize)]
pub struct BarInput {
    pub symbol: String,
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(alias = "date")]
    pub trade_date: String,
    pub high: f64,
    pub low: f64,
}

#[derive(Debug, Deserialize)]
pub struct ItemInput {
    pub symbol: String,
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(alias = "trade_dates")]
    pub dates: Vec<String>,
    pub highs: Vec<f64>,
    pub lows: Vec<f64>,
}

#[derive(Debug, Serialize)]
pub struct BatchResponse {
    pub schema_version: &'static str,
    pub algorithm: AlgorithmMetadata,
    pub results: Vec<ItemResult>,
    pub errors: Vec<BridgeError>,
}

#[derive(Debug, Serialize)]
pub struct AlgorithmMetadata {
    pub crate_name: &'static str,
    pub crate_version: &'static str,
    pub feature: &'static str,
    pub marker_filter: &'static str,
}

#[derive(Debug, Serialize)]
pub struct ItemResult {
    pub symbol: String,
    pub request_id: Option<String>,
    pub input: Option<InputMetadata>,
    pub pipeline: Option<PipelineStats>,
    pub directions: Option<Directions>,
    pub markers: Vec<Marker>,
    pub zhongshus: Vec<ZhongShu>,
    pub errors: Vec<BridgeError>,
}

#[derive(Debug, Serialize)]
pub struct InputMetadata {
    pub bars: usize,
    pub first_trade_date: String,
    pub last_trade_date: String,
    pub source: &'static str,
}

#[derive(Debug, Serialize)]
pub struct PipelineStats {
    pub bars: usize,
    pub merged: usize,
    pub fractals: usize,
    pub final_fractals: usize,
    pub strokes: usize,
    pub segments: usize,
    pub big_segments: usize,
    pub superior_segments: usize,
}

#[derive(Debug, Serialize)]
pub struct Directions {
    pub stroke: Option<&'static str>,
    pub segment: Option<&'static str>,
    pub big_segment: Option<&'static str>,
    pub superior_segment: Option<&'static str>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Marker {
    pub kind: &'static str,
    pub bar_index: usize,
    pub anchor_trade_date: String,
    pub price: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confirmed_trade_date: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ZhongShu {
    pub superior_segment_index: usize,
    pub start_bar_index: usize,
    pub end_bar_index: usize,
    pub start_trade_date: String,
    pub end_trade_date: String,
    pub zg: f64,
    pub zd: f64,
    pub gg: f64,
    pub dd: f64,
}

#[derive(Debug, Serialize)]
pub struct BridgeError {
    pub code: &'static str,
    pub message: String,
}

struct NormalizedItem {
    symbol: String,
    request_id: Option<String>,
    dates: Vec<String>,
    highs: Vec<f64>,
    lows: Vec<f64>,
    source: &'static str,
}

pub fn process(request: BatchRequest) -> BatchResponse {
    let mode = match request_mode(request.replay, request.recent_sessions) {
        Ok(mode) => mode,
        Err(error) => {
            return BatchResponse {
                schema_version: SCHEMA_VERSION,
                algorithm: algorithm_metadata(),
                results: Vec::new(),
                errors: vec![error],
            };
        }
    };
    let mut normalized = request
        .items
        .into_iter()
        .map(|item| NormalizedItem {
            symbol: item.symbol,
            request_id: item.request_id,
            dates: item.dates,
            highs: item.highs,
            lows: item.lows,
            source: "items",
        })
        .collect::<Vec<_>>();

    // BTreeMap preserves a deterministic response order while retaining the input bar order.
    let mut grouped: BTreeMap<(String, Option<String>), NormalizedItem> = BTreeMap::new();
    for bar in request.bars {
        let key = (bar.symbol.clone(), bar.request_id.clone());
        let entry = grouped.entry(key).or_insert_with(|| NormalizedItem {
            symbol: bar.symbol.clone(),
            request_id: bar.request_id.clone(),
            dates: Vec::new(),
            highs: Vec::new(),
            lows: Vec::new(),
            source: "bars",
        });
        entry.dates.push(bar.trade_date);
        entry.highs.push(bar.high);
        entry.lows.push(bar.low);
    }
    normalized.extend(grouped.into_values());

    BatchResponse {
        schema_version: SCHEMA_VERSION,
        algorithm: algorithm_metadata(),
        results: normalized
            .into_par_iter()
            .map(|item| process_item(item, mode))
            .collect(),
        errors: Vec::new(),
    }
}

pub fn invalid_request(message: impl Into<String>) -> BatchResponse {
    BatchResponse {
        schema_version: SCHEMA_VERSION,
        algorithm: algorithm_metadata(),
        results: Vec::new(),
        errors: vec![BridgeError {
            code: "invalid_json",
            message: message.into(),
        }],
    }
}

fn algorithm_metadata() -> AlgorithmMetadata {
    AlgorithmMetadata {
        crate_name: "chanlun_lean_lib",
        crate_version: "0.1.0",
        feature: "guidao",
        marker_filter: "tdx_dll mark 37: apply_sup_retreat_filter",
    }
}

#[derive(Clone, Copy)]
enum ProcessingMode {
    Live,
    Replay,
    RecentSessions(usize),
}

fn request_mode(
    replay: bool,
    recent_sessions: Option<usize>,
) -> Result<ProcessingMode, BridgeError> {
    match (replay, recent_sessions) {
        (true, Some(_)) => Err(BridgeError {
            code: "incompatible_modes",
            message: "replay and recent_sessions cannot be used together".into(),
        }),
        (_, Some(0)) => Err(BridgeError {
            code: "invalid_recent_sessions",
            message: "recent_sessions must be a positive integer".into(),
        }),
        (true, None) => Ok(ProcessingMode::Replay),
        (false, Some(sessions)) => Ok(ProcessingMode::RecentSessions(sessions)),
        (false, None) => Ok(ProcessingMode::Live),
    }
}

fn process_item(item: NormalizedItem, mode: ProcessingMode) -> ItemResult {
    let mut errors = validate(&item);
    let input = metadata(&item);
    if !errors.is_empty() {
        return ItemResult {
            symbol: item.symbol,
            request_id: item.request_id,
            input,
            pipeline: None,
            directions: None,
            markers: Vec::new(),
            zhongshus: Vec::new(),
            errors,
        };
    }

    let pipeline = ChanlunPipeline::new(item.highs.clone(), item.lows.clone());
    let zhongshus = zhongshu::detect_bigseg_zhongshus(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
    );
    let (second, third) = build_markers(&pipeline, &zhongshus);
    let stats = pipeline.stats();
    let markers = match mode {
        ProcessingMode::Live => serialize_markers(second, third, &item.dates),
        ProcessingMode::Replay => replay_markers(&item, second, third),
        ProcessingMode::RecentSessions(sessions) => {
            recent_session_markers(&item, sessions, second, third)
        }
    };
    ItemResult {
        symbol: item.symbol,
        request_id: item.request_id,
        input,
        pipeline: Some(PipelineStats {
            bars: stats.bars,
            merged: stats.merged,
            fractals: stats.fractals,
            final_fractals: stats.final_fractals,
            strokes: stats.strokes,
            segments: stats.segments,
            big_segments: stats.big_segments,
            superior_segments: stats.superior_segments,
        }),
        directions: Some(Directions {
            stroke: pipeline.strokes.last().map(|value| direction(value.is_up)),
            segment: pipeline.segments.last().map(|value| direction(value.2)),
            big_segment: pipeline.big_segments.last().map(|value| direction(value.2)),
            superior_segment: pipeline
                .superior_segments
                .last()
                .map(|value| direction(value.2)),
        }),
        markers,
        zhongshus: zhongshus
            .into_iter()
            .map(|value| ZhongShu {
                superior_segment_index: value.sup_idx,
                start_bar_index: value.start_bar,
                end_bar_index: value.end_bar,
                start_trade_date: item.dates[value.start_bar].clone(),
                end_trade_date: item.dates[value.end_bar].clone(),
                zg: value.zg,
                zd: value.zd,
                gg: value.gg,
                dd: value.dd,
            })
            .collect(),
        errors: std::mem::take(&mut errors),
    }
}

// This intentionally mirrors tdx_dll::build_markers. The DLL helper is private, so the
// bridge invokes the same public core functions in the same order.
fn build_markers(
    pipeline: &ChanlunPipeline,
    zhongshus: &[zhongshu::BigSegmentZhongShu],
) -> (Vec<SecondMarker>, Vec<zhongshu::BigSegThirdMarker>) {
    let mut second = chanlun_lean_lib::detect_second_buy_markers(
        &pipeline.strokes,
        &pipeline.segments,
        &pipeline.big_segments,
        &pipeline.superior_segments,
    );
    second.extend(chanlun_lean_lib::detect_second_sell_markers(
        &pipeline.strokes,
        &pipeline.segments,
        &pipeline.big_segments,
        &pipeline.superior_segments,
    ));
    let third = zhongshu::detect_bigseg_third_marks(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
        zhongshus,
        &pipeline.highs,
        &pipeline.lows,
    );
    let (lower_case1, down_big) = guidao::calc_bigseg_lower_band_case1(
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
    );
    let (upper_case1, up_big) = guidao::calc_bigseg_upper_band_case1(
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
    );
    let mut plus = guidao::detect_bigseg_cf_buy_markers(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
        &lower_case1,
        &down_big,
        &up_big,
    );
    plus.extend(guidao::detect_bigseg_cf_sell_markers(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
        &upper_case1,
        &up_big,
        &down_big,
    ));
    let filtered = guidao::apply_sup_retreat_filter(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
        second,
        third,
        plus,
    );
    (filtered.second_markers, filtered.third_markers)
}

fn serialize_markers(
    second: Vec<SecondMarker>,
    third: Vec<zhongshu::BigSegThirdMarker>,
    dates: &[String],
) -> Vec<Marker> {
    let mut markers = Vec::with_capacity(second.len() + third.len());
    markers.extend(second.into_iter().map(|value| Marker {
        kind: if value.is_buy {
            "second_buy"
        } else {
            "second_sell"
        },
        bar_index: value.bar_index,
        anchor_trade_date: dates[value.bar_index].clone(),
        price: value.price,
        confirmed_trade_date: None,
    }));
    markers.extend(third.into_iter().map(|value| Marker {
        kind: if value.is_buy {
            "third_buy"
        } else {
            "third_sell"
        },
        bar_index: value.bar_index,
        anchor_trade_date: dates[value.bar_index].clone(),
        price: value.price,
        confirmed_trade_date: None,
    }));
    markers.sort_by_key(|value| value.bar_index);
    markers
}

fn replay_markers(
    item: &NormalizedItem,
    final_second: Vec<SecondMarker>,
    final_third: Vec<zhongshu::BigSegThirdMarker>,
) -> Vec<Marker> {
    let mut seen = BTreeSet::new();
    let mut first_seen = Vec::new();
    for prefix_len in 1..item.dates.len() {
        let pipeline = ChanlunPipeline::new(
            item.highs[..prefix_len].to_vec(),
            item.lows[..prefix_len].to_vec(),
        );
        let zhongshus = zhongshu::detect_bigseg_zhongshus(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
        );
        let (second, third) = build_markers(&pipeline, &zhongshus);
        record_first_seen(
            &mut seen,
            &mut first_seen,
            serialize_markers(second, third, &item.dates),
            &item.dates[prefix_len - 1],
        );
    }
    record_first_seen(
        &mut seen,
        &mut first_seen,
        serialize_markers(final_second, final_third, &item.dates),
        item.dates.last().expect("validated input has one bar"),
    );
    first_seen
}

fn recent_session_markers(
    item: &NormalizedItem,
    recent_sessions: usize,
    final_second: Vec<SecondMarker>,
    final_third: Vec<zhongshu::BigSegThirdMarker>,
) -> Vec<Marker> {
    let baseline_len = item.dates.len().saturating_sub(recent_sessions);
    let mut seen = BTreeSet::new();
    let mut first_seen = Vec::new();

    if baseline_len > 0 {
        for marker in prefix_markers(item, baseline_len) {
            seen.insert((marker.kind, marker.bar_index));
        }
    }
    for prefix_len in baseline_len + 1..item.dates.len() {
        record_first_seen(
            &mut seen,
            &mut first_seen,
            prefix_markers(item, prefix_len),
            &item.dates[prefix_len - 1],
        );
    }
    record_first_seen(
        &mut seen,
        &mut first_seen,
        serialize_markers(final_second, final_third, &item.dates),
        item.dates.last().expect("validated input has one bar"),
    );
    first_seen
}

fn prefix_markers(item: &NormalizedItem, prefix_len: usize) -> Vec<Marker> {
    let pipeline = ChanlunPipeline::new(
        item.highs[..prefix_len].to_vec(),
        item.lows[..prefix_len].to_vec(),
    );
    let zhongshus = zhongshu::detect_bigseg_zhongshus(
        &pipeline.superior_segments,
        &pipeline.big_segments,
        &pipeline.segments,
        &pipeline.strokes,
    );
    let (second, third) = build_markers(&pipeline, &zhongshus);
    serialize_markers(second, third, &item.dates)
}

fn record_first_seen(
    seen: &mut BTreeSet<(&'static str, usize)>,
    first_seen: &mut Vec<Marker>,
    markers: Vec<Marker>,
    confirmed_trade_date: &str,
) {
    for mut marker in markers {
        marker.confirmed_trade_date = Some(confirmed_trade_date.to_owned());
        if seen.insert((marker.kind, marker.bar_index)) {
            first_seen.push(marker);
        }
    }
}

fn metadata(item: &NormalizedItem) -> Option<InputMetadata> {
    Some(InputMetadata {
        bars: item.dates.len(),
        first_trade_date: item.dates.first()?.clone(),
        last_trade_date: item.dates.last()?.clone(),
        source: item.source,
    })
}

fn validate(item: &NormalizedItem) -> Vec<BridgeError> {
    let mut errors = Vec::new();
    if item.symbol.trim().is_empty() {
        errors.push(BridgeError {
            code: "invalid_symbol",
            message: "symbol must not be empty".into(),
        });
    }
    if item.dates.is_empty() {
        errors.push(BridgeError {
            code: "empty_input",
            message: "at least one daily bar is required".into(),
        });
    }
    if item.dates.len() != item.highs.len() || item.dates.len() != item.lows.len() {
        errors.push(BridgeError {
            code: "length_mismatch",
            message: format!(
                "dates={}, highs={}, lows={}",
                item.dates.len(),
                item.highs.len(),
                item.lows.len()
            ),
        });
        return errors;
    }
    for (index, (&high, &low)) in item.highs.iter().zip(&item.lows).enumerate() {
        if !high.is_finite() || !low.is_finite() || high < low {
            errors.push(BridgeError {
                code: "invalid_ohlc",
                message: format!(
                    "bar {index}: high={high}, low={low}; expected finite high >= low"
                ),
            });
            break;
        }
    }
    errors
}

fn direction(is_up: bool) -> &'static str {
    if is_up {
        "up"
    } else {
        "down"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bars_are_grouped_by_symbol_and_request_id() {
        let response = process(serde_json::from_str(r#"{
            "bars": [
              {"symbol":"000001","request_id":"full","trade_date":"2026-01-02","high":10.0,"low":9.0},
              {"symbol":"000001","request_id":"prefix","trade_date":"2026-01-02","high":10.0,"low":9.0},
              {"symbol":"000001","request_id":"full","trade_date":"2026-01-05","high":11.0,"low":10.0}
            ]
        }"#).unwrap());
        assert_eq!(response.results.len(), 2);
        assert_eq!(response.results[0].request_id.as_deref(), Some("full"));
        assert_eq!(response.results[0].input.as_ref().unwrap().bars, 2);
        assert_eq!(response.results[1].request_id.as_deref(), Some("prefix"));
        assert_eq!(response.results[1].input.as_ref().unwrap().bars, 1);
    }

    #[test]
    fn invalid_item_does_not_abort_batch() {
        let response = process(
            serde_json::from_str(
                r#"{
            "items": [
              {"symbol":"BAD","dates":["2026-01-02"],"highs":[1.0],"lows":[2.0]},
              {"symbol":"OK","dates":["2026-01-02"],"highs":[2.0],"lows":[1.0]}
            ]
        }"#,
            )
            .unwrap(),
        );
        assert_eq!(response.results.len(), 2);
        assert_eq!(response.results[0].errors[0].code, "invalid_ohlc");
        assert!(response.results[1].errors.is_empty());
    }

    #[test]
    fn marker_chain_is_the_same_filter_used_by_tdx_mark_37() {
        let pipeline = ChanlunPipeline::new(
            vec![10.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0, 14.0, 16.0],
            vec![8.0, 9.0, 7.0, 10.0, 8.0, 11.0, 9.0, 12.0, 10.0, 13.0],
        );
        let zhongshus = zhongshu::detect_bigseg_zhongshus(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
        );
        let (second, third) = build_markers(&pipeline, &zhongshus);

        let mut expected_second = chanlun_lean_lib::detect_second_buy_markers(
            &pipeline.strokes,
            &pipeline.segments,
            &pipeline.big_segments,
            &pipeline.superior_segments,
        );
        expected_second.extend(chanlun_lean_lib::detect_second_sell_markers(
            &pipeline.strokes,
            &pipeline.segments,
            &pipeline.big_segments,
            &pipeline.superior_segments,
        ));
        let expected_third = zhongshu::detect_bigseg_third_marks(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
            &zhongshus,
            &pipeline.highs,
            &pipeline.lows,
        );
        let (lower_case1, down_big) = guidao::calc_bigseg_lower_band_case1(
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
        );
        let (upper_case1, up_big) = guidao::calc_bigseg_upper_band_case1(
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
        );
        let mut plus = guidao::detect_bigseg_cf_buy_markers(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
            &lower_case1,
            &down_big,
            &up_big,
        );
        plus.extend(guidao::detect_bigseg_cf_sell_markers(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
            &upper_case1,
            &up_big,
            &down_big,
        ));
        let expected = guidao::apply_sup_retreat_filter(
            &pipeline.superior_segments,
            &pipeline.big_segments,
            &pipeline.segments,
            &pipeline.strokes,
            expected_second,
            expected_third,
            plus,
        );
        assert_eq!(second, expected.second_markers);
        assert_eq!(third, expected.third_markers);
    }

    #[test]
    fn replay_keeps_a_transient_marker_and_deduplicates_its_first_visibility() {
        let marker = |kind, bar_index| Marker {
            kind,
            bar_index,
            anchor_trade_date: "2026-01-02".into(),
            price: 10.0,
            confirmed_trade_date: None,
        };
        let mut seen = BTreeSet::new();
        let mut first_seen = Vec::new();
        record_first_seen(
            &mut seen,
            &mut first_seen,
            vec![marker("second_buy", 3)],
            "2026-01-05",
        );
        // The marker remains visible on the next day, then disappears. Replay still keeps
        // the actionable first appearance rather than looking at the final-day snapshot only.
        record_first_seen(
            &mut seen,
            &mut first_seen,
            vec![marker("second_buy", 3), marker("third_buy", 5)],
            "2026-01-06",
        );
        record_first_seen(&mut seen, &mut first_seen, vec![], "2026-01-07");

        assert_eq!(first_seen.len(), 2);
        assert_eq!(first_seen[0].kind, "second_buy");
        assert_eq!(
            first_seen[0].confirmed_trade_date.as_deref(),
            Some("2026-01-05")
        );
        assert_eq!(first_seen[1].kind, "third_buy");
        assert_eq!(
            first_seen[1].confirmed_trade_date.as_deref(),
            Some("2026-01-06")
        );
    }

    #[test]
    fn absent_replay_flag_is_compatible_with_live_mode() {
        let without_flag: BatchRequest = serde_json::from_str(
            r#"{"items":[{"symbol":"OK","dates":["2026-01-02"],"highs":[2.0],"lows":[1.0]}]}"#,
        )
        .unwrap();
        let explicit_live: BatchRequest = serde_json::from_str(
            r#"{"replay":false,"items":[{"symbol":"OK","dates":["2026-01-02"],"highs":[2.0],"lows":[1.0]}]}"#,
        )
        .unwrap();
        assert_eq!(
            serde_json::to_value(process(without_flag)).unwrap(),
            serde_json::to_value(process(explicit_live)).unwrap(),
        );
    }

    #[test]
    fn recent_sessions_rejects_zero_and_full_replay_combination() {
        let zero = process(serde_json::from_str(r#"{"recent_sessions":0}"#).unwrap());
        assert_eq!(zero.results.len(), 0);
        assert_eq!(zero.errors[0].code, "invalid_recent_sessions");

        let incompatible =
            process(serde_json::from_str(r#"{"replay":true,"recent_sessions":1}"#).unwrap());
        assert_eq!(incompatible.results.len(), 0);
        assert_eq!(incompatible.errors[0].code, "incompatible_modes");
    }

    #[test]
    fn baseline_markers_are_not_reconfirmed_in_recent_window() {
        let marker = |kind, bar_index| Marker {
            kind,
            bar_index,
            anchor_trade_date: "2026-01-02".into(),
            price: 10.0,
            confirmed_trade_date: None,
        };
        let mut seen = BTreeSet::new();
        for baseline in vec![marker("second_buy", 3)] {
            seen.insert((baseline.kind, baseline.bar_index));
        }
        let mut window_first_seen = Vec::new();
        record_first_seen(
            &mut seen,
            &mut window_first_seen,
            vec![marker("second_buy", 3), marker("third_buy", 5)],
            "2026-01-10",
        );

        assert_eq!(window_first_seen.len(), 1);
        assert_eq!(window_first_seen[0].kind, "third_buy");
        assert_eq!(
            window_first_seen[0].confirmed_trade_date.as_deref(),
            Some("2026-01-10")
        );
    }

    #[test]
    fn recent_window_larger_than_history_matches_full_replay() {
        let bars = r#"[
            {"symbol":"OK","trade_date":"2026-01-02","high":10.0,"low":9.0},
            {"symbol":"OK","trade_date":"2026-01-05","high":11.0,"low":10.0},
            {"symbol":"OK","trade_date":"2026-01-06","high":10.5,"low":9.5}
        ]"#;
        let replay: BatchRequest =
            serde_json::from_str(&format!(r#"{{"replay":true,"bars":{bars}}}"#)).unwrap();
        let recent: BatchRequest =
            serde_json::from_str(&format!(r#"{{"recent_sessions":99,"bars":{bars}}}"#)).unwrap();
        assert_eq!(
            serde_json::to_value(process(replay)).unwrap(),
            serde_json::to_value(process(recent)).unwrap(),
        );
    }
}
