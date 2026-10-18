//! Parse the SQL subset of `POST /sql` (docs/sql.md) to a `Query`. Names stay as text: Python resolves them.

use crate::{Diag, ReadAgg};
use sqlparser::ast::{
    self as sql, BinaryOperator, Expr, FunctionArg, FunctionArgExpr, FunctionArguments, GroupByExpr, Ident, LimitClause, ObjectName,
    ObjectNamePart, OrderByKind, SelectItem, SetExpr, TableFactor, Value,
};
use sqlparser::dialect::DuckDbDialect;
use sqlparser::parser::{Parser, ParserError};
use sqlparser::tokenizer::Token;

/// A parsed SELECT. Each Vec keeps the order of the SQL text.
#[derive(Debug, PartialEq)]
pub struct Query {
    pub select: Vec<Item>,
    /// The WHERE terms, joined with AND: (dimension name, member names). `d = 'x'` gives one member.
    pub filters: Vec<(String, Vec<String>)>,
    pub group_by: Vec<Ref>,
    /// (reference, descending, NULLs first). NULLs are last if the SQL does not say.
    pub order_by: Vec<(Ref, bool, bool)>,
    pub limit: Option<u64>,
}

#[derive(Debug, PartialEq)]
pub enum Item {
    Dim { name: String, alias: Option<String> },
    Agg { agg: ReadAgg, metric: String, alias: Option<String> },
}

/// A name, or a 1-based position in the SELECT list.
#[derive(Debug, PartialEq)]
pub enum Ref {
    Name(String),
    Pos(u64),
}

fn unsupported(what: impl ToString) -> Diag {
    Diag::new("sql_unsupported").arg("what", what.to_string())
}

/// Err codes: sql_syntax {detail}, sql_statements (more than 1 statement), sql_unsupported {what}, sql_from {name}. The recursion
/// limit of sqlparser (50) keeps a deep expression from the stack limit of a server thread.
pub fn parse(text: &str) -> Result<Query, Diag> {
    let syntax = |e: ParserError| {
        let detail = match e {
            ParserError::TokenizerError(s) | ParserError::ParserError(s) => s,
            ParserError::RecursionLimitExceeded => "recursion limit exceeded".into(),
        };
        Diag::new("sql_syntax").arg("detail", detail)
    };
    // parse_query, not parse_statements: the parser of the other statements adds about 1 MB to the library
    let mut p = Parser::new(&DuckDbDialect {}).try_with_sql(text).map_err(syntax)?;
    let q = *p.parse_query().map_err(syntax)?;
    while p.consume_token(&Token::SemiColon) {}
    if p.peek_token().token != Token::EOF {
        return Err(Diag::new("sql_statements"));
    }
    let sql::Query { with, body, order_by, limit_clause, fetch, locks, for_clause, settings, format_clause, pipe_operators } = q;
    no(with.is_some(), "WITH")?;
    no(fetch.is_some(), "FETCH")?;
    no(!locks.is_empty(), "FOR UPDATE")?;
    no(for_clause.is_some(), "FOR")?;
    no(settings.is_some(), "SETTINGS")?;
    no(format_clause.is_some(), "FORMAT")?;
    no(!pipe_operators.is_empty(), "|>")?;
    let s = match *body {
        SetExpr::Select(s) => *s,
        SetExpr::SetOperation { op, .. } => return Err(unsupported(op)),
        other => return Err(unsupported(other)),
    };
    no(s.distinct.is_some(), "DISTINCT")?;
    no(s.top.is_some(), "TOP")?;
    no(s.into.is_some(), "INTO")?;
    no(s.exclude.is_some(), "EXCLUDE")?;
    no(!s.lateral_views.is_empty(), "LATERAL VIEW")?;
    no(s.prewhere.is_some(), "PREWHERE")?;
    no(!s.connect_by.is_empty(), "CONNECT BY")?;
    no(!s.cluster_by.is_empty(), "CLUSTER BY")?;
    no(!s.distribute_by.is_empty(), "DISTRIBUTE BY")?;
    no(!s.sort_by.is_empty(), "SORT BY")?;
    no(s.having.is_some(), "HAVING")?;
    no(!s.named_window.is_empty(), "WINDOW")?;
    no(s.qualify.is_some(), "QUALIFY")?;
    no(s.value_table_mode.is_some(), "AS VALUE")?;
    no(s.select_modifiers.is_some(), "SELECT modifiers")?;
    from(&s.from)?;
    let select = s.projection.into_iter().map(item).collect::<Result<_, _>>()?;
    let mut filters = Vec::new();
    if let Some(w) = s.selection {
        conjuncts(w, &mut filters)?;
    }
    let group_by = match s.group_by {
        GroupByExpr::Expressions(es, mods) if mods.is_empty() => es.into_iter().map(reference).collect::<Result<_, _>>()?,
        g => return Err(unsupported(g)),
    };
    let order_by = match order_by {
        None => Vec::new(),
        Some(o) if o.interpolate.is_none() => match o.kind {
            OrderByKind::Expressions(es) => es
                .into_iter()
                .map(|e| {
                    no(e.with_fill.is_some(), "WITH FILL")?;
                    Ok((reference(e.expr)?, e.options.asc == Some(false), e.options.nulls_first.unwrap_or(false)))
                })
                .collect::<Result<_, Diag>>()?,
            OrderByKind::All(_) => return Err(unsupported("ORDER BY ALL")),
        },
        Some(_) => return Err(unsupported("INTERPOLATE")),
    };
    let limit = match limit_clause {
        None => None,
        Some(LimitClause::LimitOffset { limit, offset, limit_by }) => {
            no(offset.is_some(), "OFFSET")?;
            no(!limit_by.is_empty(), "LIMIT BY")?;
            match limit {
                None => None,
                Some(e) => Some(number(&e).ok_or_else(|| unsupported(format!("LIMIT {e}")))?),
            }
        }
        Some(LimitClause::OffsetCommaLimit { .. }) => return Err(unsupported("OFFSET")),
    };
    Ok(Query { select, filters, group_by, order_by, limit })
}

fn no(present: bool, what: &str) -> Result<(), Diag> {
    if present {
        Err(unsupported(what))
    } else {
        Ok(())
    }
}

/// `FROM model` only (any case).
fn from(f: &[sql::TableWithJoins]) -> Result<(), Diag> {
    let [t] = f else {
        return Err(Diag::new("sql_from").arg("name", f.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(", ")));
    };
    no(!t.joins.is_empty(), "JOIN")?;
    match &t.relation {
        TableFactor::Table { name, alias: None, args: None, with_hints, version: None, with_ordinality: false, partitions, json_path: None, sample: None, index_hints }
            if with_hints.is_empty() && partitions.is_empty() && index_hints.is_empty() =>
        {
            match ident(name) {
                Some(i) if i.value.eq_ignore_ascii_case("model") => Ok(()),
                _ => Err(Diag::new("sql_from").arg("name", name.to_string())),
            }
        }
        r => Err(Diag::new("sql_from").arg("name", r.to_string())),
    }
}

fn ident(n: &ObjectName) -> Option<&Ident> {
    match n.0.as_slice() {
        [ObjectNamePart::Identifier(i)] => Some(i),
        _ => None,
    }
}

fn item(i: SelectItem) -> Result<Item, Diag> {
    let (e, alias) = match i {
        SelectItem::UnnamedExpr(e) => (e, None),
        SelectItem::ExprWithAlias { expr, alias } => (expr, Some(alias.value)),
        other => return Err(unsupported(other)),
    };
    match unnest(e) {
        Expr::Identifier(i) => Ok(Item::Dim { name: i.value, alias }),
        Expr::Function(f) => {
            let text = f.to_string();
            let agg = match ident(&f.name).map(|i| i.value.to_ascii_lowercase()).as_deref() {
                Some("sum") => ReadAgg::Sum,
                Some("avg") => ReadAgg::Avg,
                Some("min") => ReadAgg::Min,
                Some("max") => ReadAgg::Max,
                Some("count") => ReadAgg::Count,
                _ => return Err(unsupported(text)),
            };
            let plain = !f.uses_odbc_syntax
                && matches!(f.parameters, FunctionArguments::None)
                && f.filter.is_none()
                && f.null_treatment.is_none()
                && f.over.is_none()
                && f.within_group.is_empty();
            let FunctionArguments::List(list) = f.args else { return Err(unsupported(text)) };
            match <[FunctionArg; 1]>::try_from(list.args) {
                Ok([FunctionArg::Unnamed(FunctionArgExpr::Expr(a))]) if plain && list.duplicate_treatment.is_none() && list.clauses.is_empty() => {
                    match unnest(a) {
                        Expr::Identifier(m) => Ok(Item::Agg { agg, metric: m.value, alias }),
                        _ => Err(unsupported(text)),
                    }
                }
                _ => Err(unsupported(text)),
            }
        }
        e => Err(unsupported(e)),
    }
}

fn unnest(mut e: Expr) -> Expr {
    while let Expr::Nested(x) = e {
        e = *x;
    }
    e
}

/// A GROUP BY or ORDER BY item: a name or a 1-based position.
fn reference(e: Expr) -> Result<Ref, Diag> {
    match unnest(e) {
        Expr::Identifier(i) => Ok(Ref::Name(i.value)),
        e => number(&e).map(Ref::Pos).ok_or_else(|| unsupported(e)),
    }
}

fn number(e: &Expr) -> Option<u64> {
    match e {
        Expr::Value(v) => match &v.value {
            Value::Number(s, false) => s.parse().ok(),
            _ => None,
        },
        _ => None,
    }
}

/// `a AND b`, `d = 'm'` and `d IN ('m', ...)`.
fn conjuncts(e: Expr, out: &mut Vec<(String, Vec<String>)>) -> Result<(), Diag> {
    match unnest(e) {
        Expr::BinaryOp { left, op: BinaryOperator::And, right } => {
            conjuncts(*left, out)?;
            conjuncts(*right, out)
        }
        Expr::BinaryOp { left, op: BinaryOperator::Eq, right } => match (unnest(*left), text(&right)) {
            (Expr::Identifier(d), Some(m)) => {
                out.push((d.value, vec![m]));
                Ok(())
            }
            (l, _) => Err(unsupported(format!("{l} = {right}"))),
        },
        Expr::InList { expr, list, negated: false } => match unnest(*expr) {
            Expr::Identifier(d) => {
                let ms = list.iter().map(|x| text(x).ok_or_else(|| unsupported(x))).collect::<Result<_, _>>()?;
                out.push((d.value, ms));
                Ok(())
            }
            x => Err(unsupported(format!("{x} IN"))),
        },
        e => Err(unsupported(e)),
    }
}

fn text(e: &Expr) -> Option<String> {
    match e {
        Expr::Value(v) => match &v.value {
            Value::SingleQuotedString(s) => Some(s.clone()),
            _ => None,
        },
        Expr::Nested(x) => text(x),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn name(s: &str) -> Ref {
        Ref::Name(s.into())
    }

    fn code(text: &str) -> (&'static str, Vec<(&'static str, crate::Arg)>) {
        let d = parse(text).unwrap_err();
        (d.code, d.args)
    }

    #[test]
    fn a_full_query() {
        let q = parse(
            "SELECT Month, \"Product\" AS p, SUM(Revenue) AS rev, avg((Price)) FROM Model \
             WHERE Version = 'Actual' AND (Region IN ('East', 'West')) GROUP BY Month, 2 ORDER BY rev DESC, 1 NULLS FIRST LIMIT 10",
        )
        .unwrap();
        assert_eq!(
            q,
            Query {
                select: vec![
                    Item::Dim { name: "Month".into(), alias: None },
                    Item::Dim { name: "Product".into(), alias: Some("p".into()) },
                    Item::Agg { agg: ReadAgg::Sum, metric: "Revenue".into(), alias: Some("rev".into()) },
                    Item::Agg { agg: ReadAgg::Avg, metric: "Price".into(), alias: None },
                ],
                filters: vec![("Version".into(), vec!["Actual".into()]), ("Region".into(), vec!["East".into(), "West".into()])],
                group_by: vec![name("Month"), Ref::Pos(2)],
                order_by: vec![(name("rev"), true, false), (Ref::Pos(1), false, true)],
                limit: Some(10),
            }
        );
    }

    #[test]
    fn no_group_by_and_no_where() {
        let q = parse("select count(x) from model").unwrap();
        assert_eq!((q.select, q.filters, q.group_by, q.order_by, q.limit), (vec![Item::Agg { agg: ReadAgg::Count, metric: "x".into(), alias: None }], vec![], vec![], vec![], None));
    }

    #[test]
    fn errors_have_a_code_and_values() {
        let what = |w: &str| ("sql_unsupported", vec![("what", crate::Arg::Str(w.into()))]);
        assert_eq!(code("SELECT a FROM model HAVING SUM(b) > 1"), what("HAVING"));
        assert_eq!(code("SELECT DISTINCT a FROM model"), what("DISTINCT"));
        assert_eq!(code("SELECT COUNT(*) FROM model"), what("COUNT(*)"));
        assert_eq!(code("SELECT SUM(a + b) FROM model"), what("SUM(a + b)"));
        assert_eq!(code("SELECT a FROM model LIMIT 5 OFFSET 2"), what("OFFSET"));
        assert_eq!(code("SELECT a FROM model WHERE a <> 'x'"), what("a <> 'x'"));
        assert_eq!(code("SELECT a FROM model UNION SELECT b FROM model"), what("UNION"));
        assert_eq!(code("DELETE FROM model"), what("DELETE FROM model"));
        assert_eq!(code("SELECT a FROM model m JOIN model n ON true"), what("JOIN"));
        assert_eq!(code("SELECT a FROM sales"), ("sql_from", vec![("name", crate::Arg::Str("sales".into()))]));
        assert_eq!(code("SELECT 1; SELECT 2").0, "sql_statements");
        let (c, args) = code("SELECT FROM WHERE");
        assert_eq!((c, args.len()), ("sql_syntax", 1));
    }

    #[test]
    fn a_deep_expression_is_a_syntax_error() {
        let deep = format!("SELECT a FROM model WHERE {}a = 'x'{}", "(".repeat(5000), ")".repeat(5000));
        assert_eq!(code(&deep), ("sql_syntax", vec![("detail", crate::Arg::Str("recursion limit exceeded".into()))]));
    }
}
