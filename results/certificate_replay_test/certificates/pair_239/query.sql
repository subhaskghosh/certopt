SELECT
  p1.email
FROM PERSON AS P1
CROSS JOIN PERSON AS P2
WHERE
  p1.email = p2.email
GROUP BY
  p1.email
HAVING
  COUNT(p2.email) > 1