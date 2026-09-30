//! 式の誤りと警告。文言でなく、コードと値で返す（文言は Python の messages.MESSAGES だけが持つ）。

/// 文言に埋める値。Tuple と List は Python の tuple と list として埋める（表示が違う）。
#[derive(Clone, Debug, PartialEq)]
pub enum Arg {
    Str(String),
    Int(i64),
    Tuple(Vec<Arg>),
    List(Vec<Arg>),
    Msg(Diag), // 文言の断片
}

/// 誤りか警告 1 つ。code は messages.MESSAGES のキー。
#[derive(Clone, Debug, PartialEq)]
pub struct Diag {
    pub code: &'static str,
    pub args: Vec<(&'static str, Arg)>,
}

impl Diag {
    pub fn new(code: &'static str) -> Diag {
        Diag { code, args: Vec::new() }
    }

    pub fn arg(mut self, name: &'static str, value: impl Into<Arg>) -> Diag {
        self.args.push((name, value.into()));
        self
    }
}

/// Rust の中だけで表示するとき（テストの失敗など）の形。
impl std::fmt::Display for Diag {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}{:?}", self.code, self.args)
    }
}

impl From<&str> for Arg {
    fn from(s: &str) -> Arg {
        Arg::Str(s.to_string())
    }
}

impl From<String> for Arg {
    fn from(s: String) -> Arg {
        Arg::Str(s)
    }
}

impl From<&String> for Arg {
    fn from(s: &String) -> Arg {
        Arg::Str(s.clone())
    }
}

impl From<u32> for Arg {
    fn from(n: u32) -> Arg {
        Arg::Int(n as i64)
    }
}

impl From<Diag> for Arg {
    fn from(d: Diag) -> Arg {
        Arg::Msg(d)
    }
}
