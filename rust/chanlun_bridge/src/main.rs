use std::io::{self, Read};

fn main() {
    let mut input = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut input) {
        emit(chanlun_bridge::invalid_request(format!(
            "failed to read stdin: {error}"
        )));
        std::process::exit(1);
    }

    match serde_json::from_str::<chanlun_bridge::BatchRequest>(&input) {
        Ok(request) => emit(chanlun_bridge::process(request)),
        Err(error) => {
            emit(chanlun_bridge::invalid_request(error.to_string()));
            std::process::exit(1);
        }
    }
}

fn emit(response: chanlun_bridge::BatchResponse) {
    println!(
        "{}",
        serde_json::to_string(&response).expect("bridge response must serialize")
    );
}
