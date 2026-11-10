# Glossary

## Attachment / AttachedState

An **attachment** is the classes, manager instances, catalogs, gold resolution handles,
and live carrier picks prepared for one connection to a game process.
`reader/game/attach.py::prepare` returns the frozen `AttachedState` containing that data,
including catalog instances and stage waves. Re-attach replaces it wholesale.
Per-run state (PSD picks, baselines, counters, log cursor, and run boundaries) is not
part of an attachment. Frozen field bindings do not deep-freeze lists or catalogs.
