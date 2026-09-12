/* =============================================================================
   Languages

   One table, one tokeniser, every language the editor colours.

   The tokeniser works on the *raw* line and escapes each piece as it emits it.
   The earlier version did the opposite — escaped first, then matched — and every
   rule that consumed a byte on its own risked cutting an entity in half, which
   reached the screen as a literal `&quot;`. Escaping last makes that whole class
   of bug unreachable: a rule can never see `&`, `<` or `>` as anything but the
   single character it is.

   `state` is threaded down a file by the caller and carries whatever opened on
   an earlier line and has not closed yet — a docstring, a block comment, a
   template literal. Without it a Python module is coloured as code from its
   first docstring to its last, which is the most obvious way a highlighter can
   look broken.
   ============================================================================= */
(function (root) {
'use strict';

/* Shared keyword runs. Named so the family resemblance between, say, Java and
   C# is visible in the table rather than copied twenty lines apart. */
const C_CORE = 'auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while';
const JS_CORE = 'async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield';

/* Every language the editor knows.

   line     — what opens a comment that ends at the newline
   blocks   — [opener, closer] pairs that may run past the end of a line
   strings  — quote characters; `multi` lists the ones that may span lines
   keywords — control flow and declarations
   types    — built-in types and namespaces, coloured apart from keywords
   consts   — literals that are neither keyword nor number: true, nil, NULL
   indent   — a line matching this opens a block, so Enter indents one more
   dedent   — a line matching this closes one, so it pulls back a level
   outline  — how a symbol is recognised, for the Outline panel
   fold     — characters that nest, for bracket matching
   caseless — keywords match regardless of case (SQL)
   sigil    — a prefix that makes the word a variable ($foo, @foo)          */
const LANGS = {

  /* -- the big ones --------------------------------------------------------- */
  python: {
    label: 'Python', line: '#',
    blocks: [['"""', '"""'], ["'''", "'''"]],
    strings: ['"', "'"],
    keywords: 'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case',
    types: 'bool bytes bytearray complex dict float frozenset int list object set str tuple type super property staticmethod classmethod Exception ValueError TypeError KeyError IndexError RuntimeError OSError self cls',
    consts: 'True False None NotImplemented Ellipsis __name__ __file__ __doc__',
    indent: /:\s*(#.*)?$/, dedent: /^\s*(else|elif|except|finally|case)\b/,
    outline: [[/^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)/, 'function'], [/^\s*class\s+([A-Za-z_]\w*)/, 'class']],
  },

  javascript: {
    label: 'JavaScript', line: '//', blocks: [['/*', '*/']],
    strings: ['"', "'", '`'], multi: ['`'], regex: true,
    keywords: JS_CORE,
    types: 'Array Boolean Date Error Function JSON Map Math Number Object Promise Proxy RegExp Set String Symbol WeakMap WeakSet console document window globalThis',
    consts: 'true false null undefined NaN Infinity',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/, 'function'],
              [/^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)/, 'class'],
              [/^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)/, 'function']],
  },

  typescript: {
    label: 'TypeScript', line: '//', blocks: [['/*', '*/']],
    strings: ['"', "'", '`'], multi: ['`'], regex: true,
    keywords: JS_CORE + ' abstract as declare enum implements interface is keyof namespace never private protected public readonly satisfies type infer asserts override',
    types: 'any bigint boolean number object string symbol unknown void Array Promise Record Partial Readonly Pick Omit Map Set Date Error JSON Math console',
    consts: 'true false null undefined NaN Infinity',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/, 'function'],
              [/^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/, 'class'],
              [/^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)/, 'interface'],
              [/^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)/, 'type']],
  },

  rust: {
    label: 'Rust', line: '//', blocks: [['/*', '*/']], strings: ['"'],
    keywords: 'as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return static struct super trait type union unsafe use where while',
    types: 'bool char f32 f64 i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize str String Vec Option Result Box Rc Arc HashMap HashSet Self',
    consts: 'true false None Some Ok Err',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)/, 'struct'],
              [/^\s*(?:pub\s+)?enum\s+([A-Za-z_]\w*)/, 'enum'],
              [/^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)/, 'interface'],
              [/^\s*impl(?:<[^>]*>)?\s+([A-Za-z_]\w*)/, 'class']],
  },

  go: {
    label: 'Go', line: '//', blocks: [['/*', '*/']],
    strings: ['"', '`', "'"], multi: ['`'],
    keywords: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var',
    types: 'bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint uint8 uint16 uint32 uint64 uintptr any make new len cap append copy delete panic recover',
    consts: 'true false nil iota',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)/, 'function'],
              [/^\s*type\s+([A-Za-z_]\w*)/, 'type']],
  },

  c: {
    label: 'C', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"], pre: '#',
    keywords: C_CORE + ' restrict _Bool _Atomic',
    types: 'size_t ssize_t ptrdiff_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t FILE bool',
    consts: 'NULL true false EOF',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^[A-Za-z_][\w \t\*]*\s\*?([A-Za-z_]\w*)\s*\([^;]*$/, 'function'],
              [/^\s*(?:typedef\s+)?struct\s+([A-Za-z_]\w*)/, 'struct']],
  },

  cpp: {
    label: 'C++', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"], pre: '#',
    keywords: C_CORE + ' alignas alignof catch class concept consteval constexpr constinit co_await co_return co_yield decltype delete dynamic_cast explicit export friend mutable namespace new noexcept nullptr operator override private protected public reinterpret_cast requires static_assert static_cast template this throw try typeid typename using virtual',
    types: 'bool string wstring vector map unordered_map set unordered_set array pair tuple shared_ptr unique_ptr weak_ptr optional variant size_t std istream ostream',
    consts: 'true false nullptr NULL',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+([A-Za-z_]\w*)/, 'class'],
              [/^[\w:<>,\s\*&]+\s\*?&?([A-Za-z_]\w*)\s*\([^;]*$/, 'function']],
  },

  csharp: {
    label: 'C#', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'abstract as async await base break case catch checked class const continue default delegate do else enum event explicit extern finally fixed for foreach get goto if implicit in interface internal is lock namespace new operator out override params private protected public readonly record ref return sealed set sizeof stackalloc static struct switch this throw try typeof unchecked unsafe using var virtual volatile where while yield',
    types: 'bool byte char decimal double dynamic float int long object sbyte short string uint ulong ushort void List Dictionary IEnumerable Task Action Func Nullable',
    consts: 'true false null value',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:public|private|protected|internal)?[\w\s<>,\[\]]*\s([A-Za-z_]\w*)\s*\(/, 'function'],
              [/^\s*(?:public|private|protected|internal)?\s*(?:abstract|sealed|static|partial)?\s*(?:class|interface|struct|record|enum)\s+([A-Za-z_]\w*)/, 'class']],
  },

  java: {
    label: 'Java', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'abstract assert break case catch class const continue default do else enum extends final finally for goto if implements import instanceof interface native new package private protected public record return sealed static strictfp super switch synchronized this throw throws transient try var volatile while yield',
    types: 'boolean byte char double float int long short void String Object List Map Set ArrayList HashMap Optional Stream Integer Double Boolean Exception',
    consts: 'true false null',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:class|interface|enum|record)\s+([A-Za-z_]\w*)/, 'class'],
              [/^\s*(?:public|private|protected)[\w\s<>,\[\]]*\s([A-Za-z_]\w*)\s*\(/, 'function']],
  },

  kotlin: {
    label: 'Kotlin', line: '//', blocks: [['/*', '*/']],
    strings: ['"', "'"], blocksExtra: [['"""', '"""']],
    keywords: 'abstract actual annotation as break by catch class companion const constructor continue crossinline data do dynamic else enum expect external final finally for fun get if import in infix init inline inner interface internal is lateinit noinline object open operator out override package private protected public reified return sealed set super suspend tailrec this throw try typealias val var vararg when where while',
    types: 'Any Array Boolean Byte Char Double Float Int List Long Map MutableList MutableMap Nothing Number Pair Set Short String Unit',
    consts: 'true false null it',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:(?:private|public|internal|open|suspend|inline)\s+)*fun\s+(?:<[^>]*>\s*)?([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:(?:private|public|internal|open|data|sealed|abstract)\s+)*(?:class|object|interface)\s+([A-Za-z_]\w*)/, 'class']],
  },

  swift: {
    label: 'Swift', line: '//', blocks: [['/*', '*/']], strings: ['"'],
    keywords: 'associatedtype async await break case catch class continue default defer deinit do else enum extension fallthrough fileprivate final for func guard if import in indirect init inout internal is lazy let mutating nonmutating open operator private protocol public repeat required rethrows return self static struct subscript super switch throw throws try typealias var weak where while',
    types: 'Any AnyObject Array Bool Character Dictionary Double Float Int Optional Result Set String UInt Void Error Never',
    consts: 'true false nil',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:(?:public|private|internal|static|final|override)\s+)*func\s+([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:(?:public|private|internal|final)\s+)*(?:class|struct|enum|protocol|extension)\s+([A-Za-z_]\w*)/, 'class']],
  },

  ruby: {
    label: 'Ruby', line: '#', blocks: [['=begin', '=end']],
    strings: ['"', "'", '`'], sigil: /^[@$]{1,2}[A-Za-z_]\w*/,
    keywords: 'alias and begin break case class def defined? do else elsif end ensure for if in module next not or redo rescue retry return self super then undef unless until when while yield lambda proc require require_relative include extend attr_accessor attr_reader attr_writer',
    types: 'Array Comparable Enumerable Float Hash Integer Kernel Module Numeric Object Proc Range Regexp String Struct Symbol Time',
    consts: 'true false nil __FILE__ __LINE__',
    indent: /(\b(do|then)|[\{\[\(])\s*$|^\s*(def|class|module|if|unless|while|until|case|begin)\b(?!.*\bend\b)/,
    dedent: /^\s*(end|else|elsif|when|rescue|ensure)\b|^\s*[\}\]\)]/,
    outline: [[/^\s*def\s+([\w.?!=]+)/, 'function'], [/^\s*(?:class|module)\s+([A-Za-z_][\w:]*)/, 'class']],
  },

  php: {
    label: 'PHP', line: '//', blocks: [['/*', '*/']],
    strings: ['"', "'", '`'], sigil: /^\$[A-Za-z_]\w*/,
    keywords: 'abstract and array as break callable case catch class clone const continue declare default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile enum extends final finally fn for foreach function global goto if implements include include_once instanceof insteadof interface isset list match namespace new or print private protected public readonly require require_once return static switch throw trait try unset use var while xor yield',
    types: 'bool int float string array object mixed void never iterable callable self parent',
    consts: 'true false null TRUE FALSE NULL __DIR__ __FILE__ __LINE__ __CLASS__',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)/, 'class']],
  },

  perl: {
    label: 'Perl', line: '#', blocks: [], strings: ['"', "'", '`'],
    sigil: /^[$@%&][A-Za-z_]\w*/,
    keywords: 'and cmp continue do else elsif eq eval exit for foreach ge gt if last le local lt my ne next no not or our package redo ref require return sub unless until use wantarray while',
    types: 'chomp chop defined delete each exists grep join keys length map pop push reverse scalar shift sort splice split sprintf unshift values',
    consts: 'undef __END__ __DATA__ STDIN STDOUT STDERR',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*sub\s+([A-Za-z_]\w*)/, 'function'], [/^\s*package\s+([\w:]+)/, 'class']],
  },

  bash: {
    label: 'Shell', line: '#', blocks: [],
    strings: ['"', "'", '`'], sigil: /^\$\{?[A-Za-z_#?!@*][\w]*\}?/,
    keywords: 'if then else elif fi for while until do done case esac function select in return break continue local export readonly declare typeset source alias unalias unset shift exit trap set eval exec',
    types: 'echo printf read cd pwd ls cat grep sed awk cut sort uniq head tail find xargs test mkdir rm mv cp chmod chown curl wget git',
    consts: 'true false',
    indent: /\b(then|do|\{)\s*$|\bin\s*$/, dedent: /^\s*(fi|done|esac|else|elif|\}|;;)/,
    outline: [[/^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)/, 'function']],
  },

  powershell: {
    label: 'PowerShell', line: '#', blocks: [['<#', '#>']],
    strings: ['"', "'"], sigil: /^\$[\w:]+/,
    keywords: 'begin break catch class continue data define do dynamicparam else elseif end enum exit filter finally for foreach from function hidden if in inlinescript param process return static switch throw trap try until using var while workflow',
    types: 'Get-ChildItem Get-Content Set-Content Write-Host Write-Output Write-Error Select-Object Where-Object ForEach-Object Sort-Object Measure-Object New-Item Remove-Item Copy-Item Move-Item Test-Path Join-Path Split-Path Invoke-WebRequest Invoke-Expression Start-Process Get-Process Stop-Process Get-Command Get-Help Out-File Out-Null ConvertTo-Json ConvertFrom-Json',
    consts: '$true $false $null $PSVersionTable $PWD $Args $Error',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*function\s+([\w-]+)/, 'function'], [/^\s*class\s+([\w-]+)/, 'class']],
  },

  batch: {
    label: 'Batch', line: 'REM', lineAlt: '::', blocks: [], strings: ['"'],
    sigil: /^%[\w~]+%?|^!\w+!/, caseless: true,
    keywords: 'call echo endlocal exit for goto if else in do not set setlocal shift pause rem start title exist defined errorlevel equ neq lss leq gtr geq',
    types: 'cd dir copy move del md rd type find findstr sort xcopy robocopy tasklist taskkill',
    consts: 'on off nul',
    indent: /\(\s*$/, dedent: /^\s*\)/,
    outline: [[/^\s*:([A-Za-z_]\w*)/, 'label']],
  },

  sql: {
    label: 'SQL', line: '--', blocks: [['/*', '*/']], strings: ["'", '"'], caseless: true,
    keywords: 'add all alter and as asc begin between by case cast check column commit constraint create cross database default delete desc distinct drop else end exists foreign from full group having if in index inner insert intersect into is join key left like limit not null offset on or order outer primary references right rollback select set table then transaction union unique update using values view when where with',
    types: 'bigint binary bit blob boolean char date datetime decimal double float int integer interval json numeric real serial smallint text time timestamp uuid varchar',
    consts: 'true false null current_date current_time current_timestamp',
    outline: [[/^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure|index)\s+(?:if\s+not\s+exists\s+)?([\w."]+)/i, 'table']],
  },

  lua: {
    label: 'Lua', line: '--', blocks: [['--[[', ']]'], ['[[', ']]']], strings: ['"', "'"],
    keywords: 'and break do else elseif end false for function goto if in local nil not or repeat return then true until while',
    types: 'assert collectgarbage dofile error getmetatable ipairs load next pairs pcall print rawget rawset require select setmetatable tonumber tostring type unpack string table math io os coroutine',
    consts: 'true false nil _G _ENV self',
    indent: /\b(then|do|function|else|repeat)\s*$|[\{\(]\s*$/,
    dedent: /^\s*(end|else|elseif|until|[\}\)])/,
    outline: [[/^\s*(?:local\s+)?function\s+([\w.:]+)/, 'function']],
  },

  r: {
    label: 'R', line: '#', blocks: [], strings: ['"', "'"],
    keywords: 'break else for function if in next repeat return while library require source',
    types: 'c vector list matrix data.frame factor numeric character logical integer double complex apply lapply sapply vapply mapply print paste paste0 cat length nrow ncol dim names summary',
    consts: 'TRUE FALSE NULL NA NA_integer_ NA_real_ NA_character_ Inf NaN T F',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*([\w.]+)\s*(?:<-|=)\s*function/, 'function']],
  },

  julia: {
    label: 'Julia', line: '#', blocks: [['#=', '=#'], ['"""', '"""']], strings: ['"', "'"],
    keywords: 'abstract baremodule begin break catch const continue do else elseif end export finally for function global if import let local macro module mutable primitive quote return struct try type using while where in isa',
    types: 'Array Bool Char Dict Float32 Float64 Int Int8 Int16 Int32 Int64 Nothing Number Set String Symbol Tuple Union UInt Vector Matrix',
    consts: 'true false nothing missing Inf NaN pi',
    indent: /^\s*(function|if|for|while|struct|module|begin|let|macro|try|do)\b|[\{\[\(]\s*$/,
    dedent: /^\s*(end|else|elseif|catch|finally|[\}\]\)])/,
    outline: [[/^\s*function\s+([\w.!]+)/, 'function'], [/^\s*(?:mutable\s+)?struct\s+([\w]+)/, 'struct']],
  },

  elixir: {
    label: 'Elixir', line: '#', blocks: [['"""', '"""']], strings: ['"', "'"],
    sigil: /^[@&]\w+/,
    keywords: 'after alias and case catch cond def defdelegate defexception defguard defimpl defmacro defmodule defp defprotocol defstruct do else end fn for if import in not or quote raise receive require rescue try unless unquote use when with',
    types: 'Atom Enum Float Integer Keyword Kernel List Map MapSet Process Stream String Task Tuple GenServer Supervisor Agent',
    consts: 'true false nil __MODULE__ __ENV__',
    indent: /\b(do|fn)\s*$|[\{\[\(]\s*$/, dedent: /^\s*(end|else|[\}\]\)])/,
    outline: [[/^\s*def(?:p|macro|guard)?\s+([\w?!]+)/, 'function'], [/^\s*defmodule\s+([\w.]+)/, 'class']],
  },

  erlang: {
    label: 'Erlang', line: '%', blocks: [], strings: ['"', "'"],
    keywords: 'after and andalso band begin bnot bor bsl bsr bxor case catch cond div end fun if let not of or orelse receive rem try when xor',
    types: 'atom binary boolean float integer list map pid port reference tuple lists maps io erlang gen_server',
    consts: 'true false undefined ok error',
    indent: /(->|\bbegin|\bof)\s*$/, dedent: /^\s*(end|;|\.)/,
    outline: [[/^([a-z]\w*)\s*\(/, 'function']],
  },

  haskell: {
    label: 'Haskell', line: '--', blocks: [['{-', '-}']], strings: ['"', "'"],
    keywords: 'case class data default deriving do else forall foreign hiding if import in infix infixl infixr instance let mdo module newtype of proc rec then type where',
    types: 'Bool Char Double Either Float Int Integer IO Maybe Ordering Rational String Word Functor Monad Applicative Foldable Traversable',
    consts: 'True False Nothing Just Left Right otherwise',
    indent: /\b(where|do|of|let)\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^([a-z_]\w*)\s*::/, 'function'], [/^(?:data|newtype|type)\s+([A-Z]\w*)/, 'type']],
  },

  scala: {
    label: 'Scala', line: '//', blocks: [['/*', '*/'], ['"""', '"""']], strings: ['"', "'"],
    keywords: 'abstract case catch class def do else extends final finally for forSome given if implicit import lazy match new object override package private protected return sealed super then this throw trait try type using val var while with yield',
    types: 'Any AnyRef AnyVal Array Boolean Byte Char Double Either Float Int List Long Map Nothing Option Seq Set Short String Unit Vector Future',
    consts: 'true false null None Some Nil',
    indent: /[\{\[\(]\s*$|=\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:(?:private|protected|final|implicit|override)\s+)*def\s+([\w$]+)/, 'function'],
              [/^\s*(?:(?:private|final|sealed|abstract|case|implicit)\s+)*(?:class|object|trait)\s+([\w$]+)/, 'class']],
  },

  dart: {
    label: 'Dart', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'abstract as assert async await break case catch class const continue covariant default deferred do dynamic else enum export extends extension external factory false final finally for get if implements import in interface is late library mixin new on operator part required rethrow return sealed set show static super switch sync this throw try typedef var void while with yield',
    types: 'bool double int num String List Map Set Iterable Future Stream Object Function Widget BuildContext',
    consts: 'true false null this super',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:(?:static|final|const|abstract)\s+)*(?:[\w<>,\s]+\s+)?([A-Za-z_]\w*)\s*\(/, 'function'],
              [/^\s*(?:abstract\s+)?class\s+([A-Za-z_]\w*)/, 'class']],
  },

  zig: {
    label: 'Zig', line: '//', blocks: [], strings: ['"', "'"],
    keywords: 'align allowzero and anyframe anytype asm async await break catch comptime const continue defer else enum errdefer error export extern fn for if inline noalias noinline nosuspend opaque or orelse packed pub resume return struct suspend switch test threadlocal try union unreachable usingnamespace var volatile while',
    types: 'bool f16 f32 f64 f128 i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize void noreturn type anyerror comptime_int comptime_float std',
    consts: 'true false null undefined',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:pub\s+)?const\s+([A-Za-z_]\w*)\s*=\s*(?:struct|enum|union)/, 'struct']],
  },

  nim: {
    label: 'Nim', line: '#', blocks: [['#[', ']#'], ['"""', '"""']], strings: ['"', "'"],
    keywords: 'addr and as asm bind block break case cast concept const continue converter defer discard distinct div do elif else end enum except export finally for from func if import include interface is isnot iterator let macro method mixin mod not notin object of or out proc ptr raise ref return shl shr static template try tuple type using var when while xor yield',
    types: 'bool char string int int8 int16 int32 int64 uint float float32 float64 seq array set openArray cstring pointer auto any',
    consts: 'true false nil result',
    indent: /[:=]\s*$|[\{\[\(]\s*$/, dedent: /^\s*(else|elif|except|finally|of)\b/,
    outline: [[/^\s*(?:proc|func|method|iterator|template|macro)\s+([\w`*]+)/, 'function'],
              [/^\s*type\s+([\w*]+)/, 'type']],
  },

  vlang: {
    label: 'V', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'as asm assert atomic break const continue defer else enum fn for go goto if import in interface is lock match module mut none or pub return rlock select shared sizeof static struct type typeof union unsafe',
    types: 'bool string int i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 rune byte voidptr any map array',
    consts: 'true false none err it',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:pub\s+)?fn\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)/, 'struct']],
  },

  clojure: {
    label: 'Clojure', line: ';', blocks: [], strings: ['"'],
    keywords: 'def defn defn- defmacro defmulti defmethod defprotocol defrecord deftype definterface defstruct let letfn fn if if-not if-let when when-not when-let cond condp case do doseq dotimes loop recur for try catch finally throw ns require import use in-ns quote var set! new binding',
    types: 'list vector map set seq first rest cons conj assoc dissoc get count filter reduce apply partial comp str keyword symbol atom swap! reset! deref',
    consts: 'true false nil',
    indent: /[\(\[\{]\s*$/, dedent: /^\s*[\)\]\}]/,
    outline: [[/^\s*\(def(?:n|n-|macro|record|protocol|type)?\s+([\w!?*<>=+\-]+)/, 'function'],
              [/^\s*\(ns\s+([\w.\-]+)/, 'class']],
  },

  lisp: {
    label: 'Lisp', line: ';', blocks: [['#|', '|#']], strings: ['"'],
    keywords: 'defun defmacro defvar defparameter defconstant defclass defmethod defgeneric let let* lambda if cond case when unless do dolist dotimes loop progn setq setf quote function return-from block catch throw',
    types: 'car cdr cons list append length mapcar apply funcall format print eval nth reverse member assoc',
    consts: 't nil',
    indent: /[\(\[]\s*$/, dedent: /^\s*[\)\]]/,
    outline: [[/^\s*\(def\w*\s+([\w\-*+<>=!?]+)/, 'function']],
  },

  ocaml: {
    label: 'OCaml', line: null, blocks: [['(*', '*)']], strings: ['"', "'"],
    keywords: 'and as assert begin class constraint do done downto else end exception external for fun function functor if in include inherit initializer lazy let match method module mutable new nonrec object of open or private rec sig struct then to try type val virtual when while with',
    types: 'array bool bytes char exn float int int32 int64 list option ref string unit',
    consts: 'true false None Some',
    indent: /\b(begin|struct|sig|->|=)\s*$/, dedent: /^\s*(end|\)|\])/,
    outline: [[/^\s*let\s+(?:rec\s+)?([a-z_]\w*)/, 'function'], [/^\s*type\s+([a-z_]\w*)/, 'type']],
  },

  fsharp: {
    label: 'F#', line: '//', blocks: [['(*', '*)']], strings: ['"', "'"],
    keywords: 'abstract and as assert base begin class default delegate do done downcast downto elif else end exception extern false finally for fun function global if in inherit inline interface internal lazy let match member module mutable namespace new null of open or override private public rec return select static struct then to try type upcast use val void when while with yield',
    types: 'bool byte char decimal double float int int64 list array option obj seq string unit Map Set Async Result',
    consts: 'true false null None Some',
    indent: /[=\{\[\(]\s*$|\b(then|do|->)\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*let\s+(?:rec\s+|inline\s+)?([\w']+)/, 'function'], [/^\s*type\s+([\w']+)/, 'type']],
  },

  groovy: {
    label: 'Groovy', line: '//', blocks: [['/*', '*/'], ['"""', '"""']], strings: ['"', "'"],
    keywords: 'abstract as assert boolean break byte case catch char class const continue def default do double else enum extends final finally float for goto if implements import in instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this threadsafe throw throws trait transient try var void volatile while',
    types: 'String List Map Set Closure Object Integer Double Boolean BigDecimal File',
    consts: 'true false null it this super',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:def|void|[A-Z]\w*)\s+([a-z]\w*)\s*\(/, 'function'],
              [/^\s*(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)/, 'class']],
  },

  solidity: {
    label: 'Solidity', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'abstract anonymous as assembly break calldata catch constant constructor continue contract delete do else emit enum event external fallback for function if immutable import indexed interface internal is library mapping memory modifier new override payable pragma private public pure receive return returns revert storage struct try type unchecked using view virtual while',
    types: 'address bool bytes bytes32 int int256 string uint uint8 uint128 uint256 msg block tx now',
    consts: 'true false wei gwei ether seconds minutes hours days weeks',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*function\s+([A-Za-z_]\w*)/, 'function'],
              [/^\s*(?:contract|interface|library)\s+([A-Za-z_]\w*)/, 'class']],
  },

  terraform: {
    label: 'Terraform', line: '#', lineAlt: '//', blocks: [['/*', '*/']], strings: ['"'],
    keywords: 'resource data provider variable output module locals terraform backend provisioner lifecycle depends_on count for_each dynamic for in if else endif endfor',
    types: 'string number bool list map set object tuple any',
    consts: 'true false null var local each self path',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:resource|data|module|variable|output|provider)\s+"([^"]+)"/, 'resource']],
  },

  protobuf: {
    label: 'Protobuf', line: '//', blocks: [['/*', '*/']], strings: ['"', "'"],
    keywords: 'syntax package import option message enum service rpc returns repeated optional required oneof map reserved extend extensions stream public weak',
    types: 'double float int32 int64 uint32 uint64 sint32 sint64 fixed32 fixed64 sfixed32 sfixed64 bool string bytes',
    consts: 'true false',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:message|enum|service)\s+([A-Za-z_]\w*)/, 'class'], [/^\s*rpc\s+([A-Za-z_]\w*)/, 'function']],
  },

  graphql: {
    label: 'GraphQL', line: '#', blocks: [['"""', '"""']], strings: ['"'],
    keywords: 'query mutation subscription fragment on type interface union enum input scalar schema directive extend implements repeatable',
    types: 'ID String Int Float Boolean',
    consts: 'true false null',
    indent: /[\{\[\(]\s*$/, dedent: /^\s*[\}\]\)]/,
    outline: [[/^\s*(?:type|interface|input|enum|union|scalar)\s+([A-Za-z_]\w*)/, 'type'],
              [/^\s*(?:query|mutation|fragment)\s+([A-Za-z_]\w*)/, 'function']],
  },

  vim: {
    label: 'Vim script', line: '"', blocks: [], strings: ["'"],
    sigil: /^[abgvwlst]:\w+|^[&@$]\w+/,
    keywords: 'if elseif else endif for endfor while endwhile function endfunction return let unlet call execute source set setlocal au augroup autocmd command nnoremap inoremap vnoremap map noremap try catch finally endtry echo echom normal',
    types: 'has exists empty len split join substitute matchstr expand getline setline bufnr winnr',
    consts: 'v:true v:false v:null',
    indent: /^\s*(if|for|while|function|try|augroup)\b/, dedent: /^\s*(end|el|catch|finally|augroup END)/,
    outline: [[/^\s*fu(?:nction)?!?\s+([\w:#]+)/, 'function']],
  },

  asm: {
    label: 'Assembly', line: ';', lineAlt: '#', blocks: [], strings: ['"', "'"],
    keywords: 'mov movzx movsx lea push pop add sub mul imul div idiv inc dec and or xor not shl shr sal sar cmp test jmp je jne jz jnz jg jge jl jle ja jae jb jbe call ret leave enter nop hlt int syscall',
    types: 'byte word dword qword ptr offset section global extern db dw dd dq resb resw resd equ times align',
    consts: 'rax rbx rcx rdx rsi rdi rbp rsp eax ebx ecx edx esi edi ebp esp ax bx cx dx al bl cl dl r8 r9 r10 r11 r12 r13 r14 r15',
    outline: [[/^([A-Za-z_.][\w.]*):/, 'label']],
  },

  latex: {
    label: 'LaTeX', line: '%', blocks: [], strings: [],
    sigil: /^\\[A-Za-z@]+\*?|^\\./,
    keywords: 'begin end documentclass usepackage newcommand renewcommand section subsection subsubsection chapter part paragraph item label ref cite includegraphics input include',
    types: 'textbf textit texttt emph underline frac sqrt sum int alpha beta gamma delta',
    consts: '',
    indent: /\\begin\{/, dedent: /\\end\{/,
    outline: [[/\\(?:sub)*section\*?\{([^}]*)\}/, 'section'], [/\\chapter\*?\{([^}]*)\}/, 'section']],
  },

  dockerfile: {
    label: 'Dockerfile', line: '#', blocks: [], strings: ['"', "'"], caseless: true,
    sigil: /^\$\{?\w+\}?/,
    keywords: 'from run cmd label maintainer expose env add copy entrypoint volume user workdir arg onbuild stopsignal healthcheck shell as',
    types: '', consts: 'true false',
    outline: [[/^\s*FROM\s+(\S+)/i, 'stage']],
  },

  makefile: {
    label: 'Makefile', line: '#', blocks: [], strings: ['"', "'"],
    sigil: /^\$[\(\{]?[\w@<^?*]+[\)\}]?/,
    keywords: 'ifeq ifneq ifdef ifndef else endif include -include define endef export unexport override vpath',
    types: 'wildcard patsubst subst shell foreach filter filter-out sort dir notdir basename suffix addprefix addsuffix call eval',
    consts: '.PHONY .DEFAULT .SUFFIXES .PRECIOUS',
    outline: [[/^([A-Za-z_][\w.\-\/]*)\s*:(?!=)/, 'target']],
  },

  cmake: {
    label: 'CMake', line: '#', blocks: [['#[[', ']]']], strings: ['"'],
    sigil: /^\$\{[\w.]+\}/, caseless: true,
    keywords: 'if else elseif endif foreach endforeach while endwhile function endfunction macro endmacro return break continue',
    types: 'cmake_minimum_required project add_executable add_library target_link_libraries include_directories set option find_package install add_subdirectory message file list string',
    consts: 'ON OFF TRUE FALSE NOTFOUND',
    indent: /\(\s*$/, dedent: /^\s*\)/,
    outline: [[/^\s*(?:function|macro)\s*\(\s*([\w]+)/i, 'function']],
  },

  /* -- markup and data, each with its own line handler ---------------------- */
  html: { label: 'HTML', line: null, blocks: [['<!--', '-->']], strings: ['"', "'"], markup: true,
          indent: /<(?!\/)(?!(?:area|base|br|col|embed|hr|img|input|link|meta|source|track|wbr)\b)[a-zA-Z][^>]*(?<!\/)>\s*$/,
          dedent: /^\s*<\//,
          outline: [[/<(?:h[1-6])[^>]*>([^<]{1,80})/, 'heading'], [/\bid="([^"]+)"/, 'anchor']] },
  xml:  { label: 'XML', line: null, blocks: [['<!--', '-->']], strings: ['"', "'"], markup: true,
          indent: /<(?!\/)[a-zA-Z][^>]*(?<!\/)>\s*$/, dedent: /^\s*<\//,
          outline: [[/^\s*<([A-Za-z_][\w:.\-]*)/, 'element']] },
  css:  { label: 'CSS', line: null, lineAlt: '//', blocks: [['/*', '*/']], strings: ['"', "'"], style: true,
          indent: /\{\s*$/, dedent: /^\s*\}/,
          outline: [[/^([.#&@][^\{]{0,80}?)\s*\{/, 'rule'], [/^([a-zA-Z][^\{;]{0,80}?)\s*\{/, 'rule']] },
  markdown: { label: 'Markdown', line: null, blocks: [], strings: [], prose: true,
              outline: [[/^(#{1,6})\s+(.+)$/, 'heading']] },
  diff: { label: 'Diff', line: null, blocks: [], strings: [], patch: true,
          outline: [[/^(?:\+\+\+|---)\s+(\S+)/, 'file']] },
  json: { label: 'JSON', line: null, lineAlt: '//', blocks: [['/*', '*/']], strings: ['"'],
          keywords: '', types: '', consts: 'true false null',
          indent: /[\{\[]\s*$/, dedent: /^\s*[\}\]]/,
          outline: [[/^\s{0,4}"([^"]+)"\s*:\s*[\{\[]/, 'key']] },
  yaml: { label: 'YAML', line: '#', blocks: [], strings: ['"', "'"], data: true,
          consts: 'true false null yes no on off ~',
          indent: /:\s*$|^\s*-\s*$/,
          outline: [[/^([A-Za-z_][\w.\-]*):/, 'key']] },
  toml: { label: 'TOML', line: '#', blocks: [['"""', '"""']], strings: ['"', "'"], data: true,
          consts: 'true false',
          outline: [[/^\s*\[+([^\]]+)\]+/, 'table']] },
  ini:  { label: 'INI', line: '#', lineAlt: ';', blocks: [], strings: ['"', "'"], data: true,
          consts: 'true false yes no on off',
          outline: [[/^\s*\[([^\]]+)\]/, 'section']] },
  text: { label: 'Plain text', line: null, blocks: [], strings: [], plain: true, outline: [] },
};

/* Aliases, so a file the server labels one way still finds a definition. */
LANGS.shell = LANGS.sh = LANGS.bash;
LANGS.jsx = LANGS.javascript;
LANGS.tsx = LANGS.typescript;
LANGS.scss = LANGS.sass = LANGS.less = LANGS.css;
LANGS.htm = LANGS.vue = LANGS.svelte = LANGS.html;
LANGS.yml = LANGS.yaml;
LANGS.ps1 = LANGS.powershell;
LANGS['c++'] = LANGS.cpp;
LANGS['c#'] = LANGS.csharp;
LANGS.plaintext = LANGS.text;

/* Keyword sets are built once and cached: rebuilding a Set per line turns a
   four-thousand-line file into four thousand allocations of the same thing. */
const SETS = new Map();
function setsFor(name) {
  if (SETS.has(name)) return SETS.get(name);
  const lang = LANGS[name] || LANGS.text;
  const build = (words) => {
    const out = new Set();
    String(words || '').split(/\s+/).forEach((word) => {
      if (word) out.add(lang.caseless ? word.toLowerCase() : word);
    });
    return out;
  };
  const built = {
    keywords: build(lang.keywords), types: build(lang.types), consts: build(lang.consts),
  };
  SETS.set(name, built);
  return built;
}

const ESCAPE = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPE[c]);

/* A span, or bare text when there is nothing to say about it. */
function span(cls, text) {
  return cls ? '<span class="' + cls + '">' + escape(text) + '</span>' : escape(text);
}

const NUMBER = /^(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?\d+)?)[uUlLfFnN]*/;
const WORD = /^[A-Za-z_$][A-Za-z0-9_$]*/;
const OPERATOR = /^(?:=>|->|<=|>=|==|!=|&&|\|\||\+\+|--|\*\*|\?\?|::|\.\.\.|\.\.|<<|>>|[+\-*/%=<>!&|^~?:])+/;
const PUNCT = /^[{}()[\];,.@#]+/;

/* -- the markup, style, prose and patch handlers ---------------------------- */
/* Four grammars are different enough from "words and brackets" that pushing
   them through the general tokeniser produces something worse than no colour at
   all. Each gets the few lines it actually needs. */

function markupLine(line, lang, state) {
  let out = '';
  let i = 0;
  while (i < line.length) {
    const rest = line.slice(i);
    if (state.open === '<!--') {
      const at = rest.indexOf('-->');
      if (at === -1) { out += span('tok-com', rest); break; }
      state.open = null;
      out += span('tok-com', rest.slice(0, at + 3));
      i += at + 3;
      continue;
    }
    if (rest.startsWith('<!--')) {
      const at = rest.indexOf('-->', 4);
      if (at === -1) { state.open = '<!--'; out += span('tok-com', rest); break; }
      out += span('tok-com', rest.slice(0, at + 3));
      i += at + 3;
      continue;
    }
    const tag = rest.match(/^<\/?[A-Za-z][\w:.\-]*/);
    if (tag) {
      out += span('tok-punc', tag[0][1] === '/' ? '</' : '<') +
             span('tok-tag', tag[0].replace(/^<\/?/, ''));
      i += tag[0].length;
      continue;
    }
    const attribute = rest.match(/^\s+([A-Za-z_:@#$][\w:.\-]*)(\s*=)?/);
    if (attribute) {
      out += escape(attribute[0].slice(0, attribute[0].length - attribute[1].length -
                    (attribute[2] ? attribute[2].length : 0))) +
             span('tok-attr', attribute[1]) +
             (attribute[2] ? span('tok-op', attribute[2]) : '');
      i += attribute[0].length;
      continue;
    }
    const quoted = rest.match(/^(["'])(?:[^\\]*?)\1/) || rest.match(/^(["'])[^]*$/);
    if (quoted) {
      out += span('tok-str', quoted[0]);
      i += quoted[0].length;
      continue;
    }
    const entity = rest.match(/^&(?:[a-zA-Z]+|#\d+|#[xX][0-9a-fA-F]+);/);
    if (entity) { out += span('tok-esc', entity[0]); i += entity[0].length; continue; }
    if (rest[0] === '>' || rest[0] === '/') { out += span('tok-punc', rest[0]); i += 1; continue; }
    out += escape(rest[0]);
    i += 1;
  }
  return out;
}

function styleLine(line, lang, state) {
  let out = '';
  let i = 0;
  while (i < line.length) {
    const rest = line.slice(i);
    if (state.open === '/*') {
      const at = rest.indexOf('*/');
      if (at === -1) { out += span('tok-com', rest); break; }
      state.open = null;
      out += span('tok-com', rest.slice(0, at + 2));
      i += at + 2;
      continue;
    }
    if (rest.startsWith('/*')) {
      const at = rest.indexOf('*/', 2);
      if (at === -1) { state.open = '/*'; out += span('tok-com', rest); break; }
      out += span('tok-com', rest.slice(0, at + 2));
      i += at + 2;
      continue;
    }
    if (rest.startsWith('//')) { out += span('tok-com', rest); break; }
    const at_rule = rest.match(/^@[\w-]+/);
    if (at_rule) { out += span('tok-key', at_rule[0]); i += at_rule[0].length; continue; }
    const variable = rest.match(/^--[\w-]+|^\$[\w-]+/);
    if (variable) { out += span('tok-var', variable[0]); i += variable[0].length; continue; }
    const quoted = rest.match(/^"(?:\\.|[^"\\])*"?|^'(?:\\.|[^'\\])*'?/);
    if (quoted) { out += span('tok-str', quoted[0]); i += quoted[0].length; continue; }
    const colour = rest.match(/^#[0-9a-fA-F]{3,8}\b/);
    if (colour) { out += span('tok-num', colour[0]); i += colour[0].length; continue; }
    const number = rest.match(/^-?\d*\.?\d+(?:px|em|rem|%|vh|vw|s|ms|deg|fr|ch|ex|pt|vmin|vmax)?/);
    if (number && /\d/.test(number[0])) { out += span('tok-num', number[0]); i += number[0].length; continue; }
    const selector = rest.match(/^[.#][\w-]+|^::?[\w-]+/);
    if (selector) { out += span('tok-type', selector[0]); i += selector[0].length; continue; }
    const word = rest.match(/^[a-zA-Z-][\w-]*/);
    if (word) {
      const next = rest.slice(word[0].length).match(/^\s*[:(]/);
      out += span(next ? (next[0].trim() === '(' ? 'tok-fn' : 'tok-attr') : '', word[0]);
      i += word[0].length;
      continue;
    }
    const punctuation = rest.match(/^[{}();:,>+~*]+/);
    if (punctuation) { out += span('tok-punc', punctuation[0]); i += punctuation[0].length; continue; }
    out += escape(rest[0]);
    i += 1;
  }
  return out;
}

function proseLine(line, lang, state) {
  if (state.open === '```') {
    if (/^\s*```/.test(line)) { state.open = null; return span('tok-key', line); }
    return span('tok-str', line);
  }
  if (/^\s*```/.test(line)) { state.open = '```'; return span('tok-key', line); }
  if (/^\s{0,3}#{1,6}\s/.test(line)) return span('tok-head', line);
  if (/^\s{0,3}>/.test(line)) return span('tok-com', line);
  if (/^\s{0,3}([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) return span('tok-punc', line);

  let out = '';
  let i = 0;
  while (i < line.length) {
    const rest = line.slice(i);
    const code = rest.match(/^`[^`]*`/);
    if (code) { out += span('tok-str', code[0]); i += code[0].length; continue; }
    const link = rest.match(/^\[[^\]]*\]\([^)]*\)/);
    if (link) { out += span('tok-fn', link[0]); i += link[0].length; continue; }
    const strong = rest.match(/^\*\*[^*]+\*\*|^__[^_]+__/);
    if (strong) { out += span('tok-key', strong[0]); i += strong[0].length; continue; }
    const emphasis = rest.match(/^\*[^*\s][^*]*\*|^_[^_\s][^_]*_/);
    if (emphasis) { out += span('tok-type', emphasis[0]); i += emphasis[0].length; continue; }
    const bullet = i === 0 && rest.match(/^\s*(?:[-*+]|\d+\.)\s/);
    if (bullet) { out += span('tok-punc', bullet[0]); i += bullet[0].length; continue; }
    out += escape(rest[0]);
    i += 1;
  }
  return out;
}

function patchLine(line) {
  if (/^(?:\+\+\+|---)/.test(line)) return span('tok-key', line);
  if (/^@@/.test(line)) return span('tok-fn', line);
  if (line[0] === '+') return span('tok-add', line);
  if (line[0] === '-') return span('tok-del', line);
  if (/^(?:diff|index|new file|deleted file|similarity|rename)/.test(line)) return span('tok-com', line);
  return escape(line);
}

function dataLine(line, name, lang, state) {
  // YAML, TOML and INI: a key, a separator, a value. Colouring the key is worth
  // more than anything a general tokeniser would find in the value.
  const section = line.match(/^\s*\[+[^\]]*\]+/);
  if (section) return span('tok-type', section[0]) + escape(line.slice(section[0].length));

  const bullet = line.match(/^\s*-\s/);
  let out = '';
  let head = 0;
  if (bullet && name === 'yaml') { out += span('tok-punc', bullet[0]); head = bullet[0].length; }

  const rest = line.slice(head);
  const pair = rest.match(/^([\w.$"'\-\/ ]+?)(\s*[:=]\s*)/);
  if (pair) {
    out += span('tok-attr', pair[1]) + span('tok-op', pair[2]);
    head += pair[0].length;
  }
  const value = line.slice(head);
  if (!value) return out;
  if (lang.line && value.trimStart().startsWith(lang.line)) return out + span('tok-com', value);

  const sets = setsFor(name);
  const quoted = value.match(/^\s*(["'])(?:\\.|(?!\1)[^\\])*\1/);
  if (quoted) return out + span('tok-str', quoted[0]) + escape(value.slice(quoted[0].length));
  if (sets.consts.has(value.trim())) return out + span('tok-const', value);
  if (/^\s*-?[\d.]+\s*$/.test(value)) return out + span('tok-num', value);
  const trailing = value.indexOf(' ' + (lang.line || '\u0000'));
  if (lang.line && trailing > -1) {
    return out + escape(value.slice(0, trailing)) + span('tok-com', value.slice(trailing));
  }
  return out + escape(value);
}

/* -- the general tokeniser -------------------------------------------------- */
function generalLine(line, name, lang, state) {
  const sets = setsFor(name);
  const quotes = lang.strings || [];
  const multi = lang.multi || [];
  const blocks = lang.blocks || [];
  let out = '';
  let i = 0;

  while (i < line.length) {
    const rest = line.slice(i);

    // Indentation and runs of space are emitted verbatim: cheaper than letting
    // every rule below reject them one character at a time.
    const gap = rest.match(/^\s+/);
    if (gap) { out += escape(gap[0]); i += gap[0].length; continue; }

    // A block that may run past the end of the line.
    let opened = null;
    for (let b = 0; b < blocks.length; b++) {
      if (rest.startsWith(blocks[b][0])) { opened = blocks[b]; break; }
    }
    if (opened) {
      const cls = /^[/({#<=-]/.test(opened[0]) && opened[0] !== '"""' ? 'tok-com' : 'tok-str';
      const at = rest.indexOf(opened[1], opened[0].length);
      if (at === -1 || opened[0] === opened[1] && rest.length === opened[0].length) {
        state.open = { close: opened[1], cls: cls };
        out += span(cls, rest);
        break;
      }
      const chunk = rest.slice(0, at + opened[1].length);
      out += span(cls, chunk);
      i += chunk.length;
      continue;
    }

    // Line comments. `lineAlt` covers the languages with two of them.
    if (lang.line && rest.startsWith(lang.line)) { out += span('tok-com', rest); break; }
    if (lang.lineAlt && rest.startsWith(lang.lineAlt)) { out += span('tok-com', rest); break; }

    // A preprocessor line: `#include`, `#define`, and friends.
    if (lang.pre && rest[0] === lang.pre && line.slice(0, i).trim() === '') {
      const directive = rest.match(/^#\s*[a-z]+/);
      if (directive) { out += span('tok-key', directive[0]); i += directive[0].length; continue; }
    }

    // A sigil-led variable: $foo, @ivar, %hash, ${VAR}.
    if (lang.sigil) {
      const variable = rest.match(lang.sigil);
      if (variable) { out += span('tok-var', variable[0]); i += variable[0].length; continue; }
    }

    // Strings. Scanned by hand rather than by regex so a backslash escape is
    // honoured and an unterminated quote does not swallow the file.
    const quote = quotes.indexOf(rest[0]) === -1 ? null : rest[0];
    if (quote) {
      let end = 1;
      let closed = false;
      while (end < rest.length) {
        if (rest[end] === '\\') { end += 2; continue; }
        if (rest[end] === quote) { end += 1; closed = true; break; }
        end += 1;
      }
      if (!closed && multi.indexOf(quote) !== -1) {
        state.open = { close: quote, cls: 'tok-str' };
        out += span('tok-str', rest);
        break;
      }
      out += span('tok-str', rest.slice(0, end));
      i += end;
      continue;
    }

    const number = rest.match(NUMBER);
    if (number && number[0]) {
      const before = i === 0 ? '' : line[i - 1];
      if (!/[A-Za-z_$]/.test(before)) {
        out += span('tok-num', number[0]);
        i += number[0].length;
        continue;
      }
    }

    const word = rest.match(WORD);
    if (word) {
      const text = word[0];
      const probe = lang.caseless ? text.toLowerCase() : text;
      const after = rest.slice(text.length);
      let cls = '';
      if (sets.consts.has(probe)) cls = 'tok-const';
      else if (sets.keywords.has(probe)) cls = 'tok-key';
      else if (sets.types.has(probe)) cls = 'tok-type';
      else if (/^\s*\(/.test(after)) cls = 'tok-fn';
      else if (/^[A-Z][A-Za-z0-9_]*$/.test(text) && text.length > 1) cls = 'tok-type';
      out += span(cls, text);
      i += text.length;
      continue;
    }

    const operator = rest.match(OPERATOR);
    if (operator) { out += span('tok-op', operator[0]); i += operator[0].length; continue; }

    const punctuation = rest.match(PUNCT);
    if (punctuation) { out += span('tok-punc', punctuation[0]); i += punctuation[0].length; continue; }

    out += escape(rest[0]);
    i += 1;
  }
  return out;
}

/* -- the entry point -------------------------------------------------------- */
/**
 * Colour one line.
 *
 * @param {string} line      the raw line, unescaped
 * @param {string} name      a language name from LANGS, or anything else for plain
 * @param {object} state     carried between lines; `{}` for a standalone line
 * @returns {string}         HTML: escaped text, wrapped in fixed-class spans
 */
function highlight(line, name, state) {
  state = state || {};
  const lang = LANGS[name];
  if (!lang || lang.plain) return escape(line);
  if (lang.patch) return patchLine(line);
  if (lang.prose) return proseLine(line, lang, state);
  if (lang.markup) return markupLine(line, lang, state);
  if (lang.style) return styleLine(line, lang, state);
  if (lang.data) return dataLine(line, name, lang, state);
  return continued(line, name, lang, state);
}

/* Continuation is handled here rather than inside the loop so the loop never
   has to reason about a block it did not open. */
function continued(line, name, lang, state) {
  if (!state.open) return generalLine(line, name, lang, state);
  const open = state.open;
  const at = line.indexOf(open.close);
  // Never padded: the editor draws this layer directly behind a textarea, and
  // one invented space is enough to slide every character after it out of
  // register with the caret.
  if (at === -1) return span(open.cls, line);
  state.open = null;
  const head = line.slice(0, at + open.close.length);
  return span(open.cls, head) + generalLine(line.slice(head.length), name, lang, state);
}

/* -- what the editor asks the table ---------------------------------------- */

/** The token that comments a line out, for Ctrl+/. */
function commentToken(name) {
  const lang = LANGS[name];
  if (!lang) return '';
  if (lang.line) return lang.line;
  if (lang.markup) return '<!--';
  if (lang.style || lang.prose) return '/*';
  return '';
}

/** The pair that comments a block out, when there is no line comment. */
function blockComment(name) {
  const lang = LANGS[name];
  if (!lang || lang.line) return null;
  if (lang.markup) return ['<!--', '-->'];
  if (lang.style) return ['/*', '*/'];
  if (lang.prose) return ['<!--', '-->'];
  return null;
}

/** Whether the line just ended opens a block, so Enter should indent. */
function opensBlock(name, line) {
  const lang = LANGS[name];
  return !!(lang && lang.indent && lang.indent.test(line));
}

/** Whether the line being typed closes a block, so it should pull back. */
function closesBlock(name, line) {
  const lang = LANGS[name];
  return !!(lang && lang.dedent && lang.dedent.test(line));
}

/** Human-readable name for the status bar. */
function label(name) {
  const lang = LANGS[name];
  return (lang && lang.label) || (name ? name : 'Plain text');
}

/** Every language the editor knows, sorted, for the status-bar picker. */
function catalogue() {
  const seen = new Set();
  const out = [];
  Object.keys(LANGS).forEach((key) => {
    const lang = LANGS[key];
    if (seen.has(lang)) return;
    seen.add(lang);
    out.push({ key: key, label: lang.label || key });
  });
  out.sort((a, b) => a.label.localeCompare(b.label));
  return out;
}

/** Symbols in a file, for the Outline panel. */
function outline(text, name) {
  const lang = LANGS[name];
  if (!lang || !lang.outline || !lang.outline.length) return [];
  const found = [];
  const lines = text.split('\n');
  for (let i = 0; i < lines.length && found.length < 600; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    for (let r = 0; r < lang.outline.length; r++) {
      const match = line.match(lang.outline[r][0]);
      if (match && match[1]) {
        found.push({
          name: (match[2] || match[1]).trim().slice(0, 80),
          kind: lang.outline[r][1],
          line: i + 1,
          depth: Math.min(6, Math.floor((line.length - line.trimStart().length) / 2)),
        });
        break;
      }
    }
  }
  return found;
}

/* Bracket pairs the editor closes for you, and matches against. */
const PAIRS = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
const CLOSERS = { ')': '(', ']': '[', '}': '{' };

/** Everything the Problems panel reports. Deliberately shallow: these are the
    mistakes a highlighter can see without becoming a compiler, and reporting
    only those is better than reporting confident nonsense about the rest. */
function lint(text, name) {
  const lang = LANGS[name] || LANGS.text;
  const lines = text.split('\n');
  const problems = [];
  const stack = [];
  const state = {};
  let tabs = 0;
  let spaces = 0;

  lines.forEach((line, index) => {
    const number = index + 1;

    if (/^(?:<{7}|={7}|>{7})/.test(line)) {
      problems.push({ line: number, level: 'error', message: 'Unresolved merge conflict marker.' });
    }
    const todo = line.match(/\b(TODO|FIXME|XXX|HACK)\b:?(.*)/);
    if (todo) {
      problems.push({ line: number, level: 'info', message: todo[1] + (todo[2] ? ':' + todo[2].slice(0, 90) : '') });
    }
    if (/[ \t]+$/.test(line)) {
      problems.push({ line: number, level: 'hint', message: 'Trailing whitespace.' });
    }
    if (/^\t/.test(line)) tabs += 1;
    else if (/^ {2}/.test(line)) spaces += 1;

    // Brackets, counted only outside strings and comments — which is exactly
    // what the tokeniser already works out, so it is reused rather than redone.
    if (!lang.plain && !lang.prose && !lang.patch) {
      const coloured = highlight(line, name, state);
      const bare = coloured.replace(/<span class="tok-(?:str|com)">[^]*?<\/span>/g, '')
                           .replace(/<[^>]+>/g, '')
                           .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
                           .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
      for (let c = 0; c < bare.length; c++) {
        const ch = bare[c];
        if (ch === '(' || ch === '[' || ch === '{') stack.push({ ch: ch, line: number });
        else if (ch === ')' || ch === ']' || ch === '}') {
          const top = stack.pop();
          if (!top) {
            problems.push({ line: number, level: 'error', message: 'Closing ' + ch + ' with nothing open.' });
          } else if (PAIRS[top.ch] !== ch) {
            problems.push({ line: number, level: 'error',
                            message: 'Closing ' + ch + ' does not match the ' + top.ch + ' opened on line ' + top.line + '.' });
          }
        }
      }
    }
  });

  stack.slice(0, 6).forEach((open) => {
    problems.push({ line: open.line, level: 'error', message: open.ch + ' is never closed.' });
  });
  if (tabs && spaces) {
    problems.push({ line: 1, level: 'warn', message: 'This file indents with both tabs and spaces.' });
  }
  problems.sort((a, b) => a.line - b.line);
  return problems.slice(0, 300);
}

root.Lang = {
  LANGS: LANGS, PAIRS: PAIRS, CLOSERS: CLOSERS,
  highlight: highlight, escape: escape,
  commentToken: commentToken, blockComment: blockComment,
  opensBlock: opensBlock, closesBlock: closesBlock,
  label: label, catalogue: catalogue, outline: outline, lint: lint,
};

}(window));
