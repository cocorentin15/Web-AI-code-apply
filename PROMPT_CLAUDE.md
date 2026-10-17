# RÈGLES DE SORTIE (réponse appliquée automatiquement par script)

1. Structure : analyse courte AVANT les modifications (fichiers touchés, comportement, risques ; 3 à 6 lignes), puis UNIQUEMENT des blocs. Aucun texte entre les blocs ni après le dernier : le script l'absorberait dans le contenu du dernier fichier ou du dernier REPLACE.
2. Mode : choisis le moins coûteux en tokens ; à égalité, MODE SCRIPT (plus robuste). Fichier nouveau ou très modifié = MODE SCRIPT. Patch = fichier existant uniquement.
3. Formats exacts (chaque marqueur seul sur sa ligne, en début de ligne, sans fence markdown ; les opérations s'exécutent dans l'ordre) :
- MODE SCRIPT : `=== FILE: chemin ===`, puis le fichier COMPLET, puis `=== END FILE ===` (obligatoire après chaque fichier).
- MODE PETITE IA, un patch : `FILE: chemin`, `FIND`, bloc exact, `REPLACE`, nouveau bloc (REPLACE vide = bloc supprimé).
- Plusieurs patches : `FILE: chemin`, puis pour chaque patch `PATCH n`, `FIND`, bloc exact, `REPLACE`, nouveau bloc.
- Suppression d'un fichier : `=== DELETE FILE: chemin ===`, rien d'autre.
4. FIND : copié verbatim depuis le contexte fourni (indentation exacte) et unique dans le fichier ; ajoute des lignes voisines si besoin.
5. Interdits : pseudo-code, "...", "reste inchangé", numéros de ligne, fences markdown autour des blocs, fichier complet abrégé ou partiel.
6. Un fichier dont le contenu contiendrait lui-même une ligne d'en-tête du format (début de ligne : `=== FILE`, `=== END FILE`, `=== DELETE FILE`, `FILE:`) est à éviter ou à reformuler.
