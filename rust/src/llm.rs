use serde::{Deserialize, Serialize};
use url::{Host, Url};

pub const SYSTEM_PROMPT: &str = "Return exactly one concise terminal tab label. Preserve distinguishing project and activity meaning. Do not invent facts. Output only the label.";
pub const MAX_SOURCE_BYTES: usize = 4 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 64 * 1024;
pub const MAX_OUTPUT_TOKENS: u16 = 64;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OllamaConfig {
    pub base_url: String,
    pub model: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChatRequest {
    pub url: String,
    pub body: Vec<u8>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum RequestError {
    InvalidBaseUrl,
    EmptyModel,
    InvalidSource,
    Serialization,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ResponseError {
    HttpStatus,
    OversizedBody,
    InvalidUtf8,
    InvalidJson,
    MissingContent,
    InvalidLabel,
}

#[derive(Serialize)]
struct CompletionRequest<'a> {
    model: &'a str,
    messages: [Message<'a>; 2],
    stream: bool,
    max_tokens: u16,
}

#[derive(Serialize)]
struct Message<'a> {
    role: &'a str,
    content: String,
}

#[derive(Deserialize)]
struct CompletionResponse {
    #[serde(default)]
    choices: Vec<Choice>,
}

#[derive(Deserialize)]
struct Choice {
    message: Option<ResponseMessage>,
}

#[derive(Deserialize)]
struct ResponseMessage {
    content: Option<String>,
}

pub fn normalize_source(source: &str) -> String {
    source.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn chat_completions_url(base_url: &str) -> Result<String, RequestError> {
    let mut parsed = Url::parse(base_url).map_err(|_| RequestError::InvalidBaseUrl)?;
    if parsed.scheme() != "http"
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err(RequestError::InvalidBaseUrl);
    }

    let is_loopback = match parsed.host() {
        Some(Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(host)) => host.octets() == [127, 0, 0, 1],
        Some(Host::Ipv6(host)) => host.is_loopback(),
        None => false,
    };
    if !is_loopback {
        return Err(RequestError::InvalidBaseUrl);
    }

    let normalized_path = parsed.path().trim_end_matches('/');
    if !matches!(normalized_path, "" | "/v1") {
        return Err(RequestError::InvalidBaseUrl);
    }
    let path = if normalized_path.is_empty() {
        "/chat/completions".to_owned()
    } else {
        format!("{normalized_path}/chat/completions")
    };
    parsed.set_path(&path);
    Ok(parsed.to_string())
}

pub fn build_chat_request(
    config: &OllamaConfig,
    source: &str,
    max_chars: usize,
) -> Result<ChatRequest, RequestError> {
    let source = normalize_source(source);
    if source.is_empty() || source.len() > MAX_SOURCE_BYTES || max_chars == 0 {
        return Err(RequestError::InvalidSource);
    }
    if config.model.trim().is_empty() {
        return Err(RequestError::EmptyModel);
    }

    let payload = CompletionRequest {
        model: config.model.trim(),
        messages: [
            Message {
                role: "system",
                content: SYSTEM_PROMPT.to_owned(),
            },
            Message {
                role: "user",
                content: format!("Shorten this title to at most {max_chars} characters: {source}"),
            },
        ],
        stream: false,
        max_tokens: MAX_OUTPUT_TOKENS,
    };
    let body = serde_json::to_vec(&payload).map_err(|_| RequestError::Serialization)?;
    Ok(ChatRequest {
        url: chat_completions_url(&config.base_url)?,
        body,
    })
}

pub fn validate_chat_response(
    status: u16,
    body: &[u8],
    max_chars: usize,
) -> Result<String, ResponseError> {
    if !(200..300).contains(&status) {
        return Err(ResponseError::HttpStatus);
    }
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(ResponseError::OversizedBody);
    }
    let text = std::str::from_utf8(body).map_err(|_| ResponseError::InvalidUtf8)?;
    let response: CompletionResponse =
        serde_json::from_str(text).map_err(|_| ResponseError::InvalidJson)?;
    let content = response
        .choices
        .first()
        .and_then(|choice| choice.message.as_ref())
        .and_then(|message| message.content.as_deref())
        .map(str::trim)
        .ok_or(ResponseError::MissingContent)?;
    if !is_valid_label(content, max_chars) {
        return Err(ResponseError::InvalidLabel);
    }
    Ok(content.to_owned())
}

fn is_valid_label(label: &str, max_chars: usize) -> bool {
    !label.is_empty()
        && label.chars().count() <= max_chars
        && !label.chars().any(is_forbidden_label_character)
}

fn is_forbidden_label_character(character: char) -> bool {
    character.is_control()
        || matches!(
            character,
            '\u{00ad}'
                | '\u{061c}'
                | '\u{180e}'
                | '\u{200b}'..='\u{200f}'
                | '\u{2028}'..='\u{202e}'
                | '\u{2060}'..='\u{2064}'
                | '\u{2066}'..='\u{206f}'
                | '\u{feff}'
                | '\u{fff9}'..='\u{fffb}'
                | '\u{e0001}'
                | '\u{e0020}'..='\u{e007f}'
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct UrlCase {
        input: String,
        valid: bool,
        normalized: Option<String>,
    }

    #[test]
    fn shared_loopback_url_cases_conform() {
        let cases: Vec<UrlCase> = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/loopback_url_cases.json"
        )))
        .unwrap();
        for case in cases {
            let actual = chat_completions_url(&case.input);
            assert_eq!(actual.is_ok(), case.valid, "{}", case.input);
            if let Some(expected) = case.normalized {
                assert_eq!(actual.unwrap(), expected, "{}", case.input);
            }
        }
    }

    #[test]
    fn request_is_bounded_non_streaming_and_metadata_only() {
        let request = build_chat_request(
            &OllamaConfig {
                base_url: "http://localhost:11434/v1/".into(),
                model: "llama3.2".into(),
            },
            "implement native Ollama compression",
            24,
        )
        .unwrap();
        let body: serde_json::Value = serde_json::from_slice(&request.body).unwrap();
        assert_eq!(request.url, "http://localhost:11434/v1/chat/completions");
        assert_eq!(body["stream"], false);
        assert_eq!(body["max_tokens"], MAX_OUTPUT_TOKENS);
        assert_eq!(body["messages"][0]["content"], SYSTEM_PROMPT);
        let serialized = String::from_utf8(request.body).unwrap();
        assert!(serialized.contains("Preserve distinguishing project and activity meaning"));
        assert!(serialized.contains("Do not invent facts"));
        assert!(!serialized.contains("cwd"));
        assert!(!serialized.contains("tab_id"));
    }

    #[test]
    fn response_validation_rejects_every_unsafe_shape() {
        let valid = br#"{"choices":[{"message":{"content":"native Ollama"}}]}"#;
        assert_eq!(
            validate_chat_response(200, valid, 16).unwrap(),
            "native Ollama"
        );
        assert_eq!(
            validate_chat_response(500, valid, 16),
            Err(ResponseError::HttpStatus)
        );
        assert_eq!(
            validate_chat_response(200, &vec![b'x'; MAX_RESPONSE_BYTES + 1], 16),
            Err(ResponseError::OversizedBody)
        );
        assert_eq!(
            validate_chat_response(200, &[0xff], 16),
            Err(ResponseError::InvalidUtf8)
        );
        assert_eq!(
            validate_chat_response(200, b"{}", 16),
            Err(ResponseError::MissingContent)
        );
        for content in [
            "",
            "two\nlines",
            "too many characters",
            "hidden\u{200b}text",
            "bidi\u{202e}",
        ] {
            let body = serde_json::json!({"choices": [{"message": {"content": content}}]});
            assert_eq!(
                validate_chat_response(200, &serde_json::to_vec(&body).unwrap(), 10),
                Err(ResponseError::InvalidLabel),
                "{content:?}"
            );
        }
    }

    #[test]
    fn normalized_source_enforces_four_kibibyte_ceiling() {
        let config = OllamaConfig {
            base_url: "http://127.0.0.1:11434/v1".into(),
            model: "llama3.2".into(),
        };
        assert!(build_chat_request(&config, &"x".repeat(MAX_SOURCE_BYTES), 24).is_ok());
        assert_eq!(
            build_chat_request(&config, &"x".repeat(MAX_SOURCE_BYTES + 1), 24),
            Err(RequestError::InvalidSource)
        );
    }
}
