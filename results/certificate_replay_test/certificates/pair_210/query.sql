SELECT DISTINCT
  a.email AS EMAIL
FROM (
  SELECT
    *
  FROM PERSON
) AS A
CROSS JOIN (
  SELECT
    *
  FROM PERSON
) AS B
WHERE
  a.id <> b.id AND a.email = b.email