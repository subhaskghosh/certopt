SELECT DISTINCT
  a.email
FROM PERSON AS A
CROSS JOIN PERSON AS B
WHERE
  a.id <> b.id AND a.email = b.email